"""What is actually listening on the target host.

The authoritative source is `/ports.json`, served read-only by
`harbor-console-web` on the host itself, because only the host can see
loopback-bound listeners and non-Docker ones (sshd, tailscaled) -- an allocator
blind to those would eventually hand one out. When it is unreachable the CLI
refuses to grant rather than guessing.

`probe_live` below offers a TCP probe over the tailnet as a possible fallback,
but nothing wires it: no caller outside its own test reaches it, and the
`LiveState` it returns is marked `complete=False`, which is exactly the state
the CLI declines to grant on. It is provided, not used.
"""

from __future__ import annotations

import json
import socket
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from harbor_console.ports.keys import ANY_ADDR, addrs_overlap


class LiveUnavailable(Exception):
    """Live host state could not be obtained."""


@dataclass(frozen=True)
class Listener:
    """One socket listening on the host. `container` is None for non-Docker."""

    addr: str
    port: int
    container: str | None


@dataclass(frozen=True)
class LiveState:
    """A snapshot of listening sockets. `complete` is False for TCP probing."""

    host: str
    listeners: tuple[Listener, ...]
    complete: bool

    def is_listening(self, addr: str, port: int) -> bool:
        """True when anything on this host contends for (addr, port)."""
        return any(
            listener.port == port and addrs_overlap(listener.addr, addr)
            for listener in self.listeners
        )

    def container_on(self, port: int, addr: str | None = None) -> str | None:
        """The container holding a port, when known.

        Without ``addr`` this answers about the port number alone, matching the
        first listener found -- the original behaviour, kept for callers that
        have no address to check against. With ``addr`` it answers only for a
        listener whose bind address overlaps it (``addrs_overlap`` semantics: a
        listener on ``0.0.0.0`` contends with any addr, and vice versa), since a
        stranger's listener on an unrelated address says nothing about who owns
        this one.
        """
        for listener in self.listeners:
            if listener.port != port:
                continue
            if addr is not None and not addrs_overlap(listener.addr, addr):
                continue
            return listener.container
        return None


def fetch_live(
    url: str,
    timeout: float = 5.0,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> LiveState:
    """Read authoritative host state from harbor-console-web's /ports.json."""
    try:
        with opener(url, timeout=timeout) as response:  # type: ignore[union-attr]
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise LiveUnavailable(f"{url}: {exc}") from exc

    try:
        listeners = []
        for entry in payload["listening"]:
            port_value = entry["port"]
            # Reject non-integer types (floats, strings, booleans, etc.)
            # bool is a subclass of int, so must check it explicitly
            if not isinstance(port_value, int) or isinstance(port_value, bool):
                raise ValueError(f"port must be an integer, not {type(port_value).__name__}: {port_value!r}")
            listeners.append(
                Listener(
                    addr=entry["addr"],
                    port=port_value,
                    container=entry.get("container"),
                )
            )
        listeners = tuple(listeners)
        host = payload["host"]
    except (KeyError, TypeError, ValueError) as exc:
        raise LiveUnavailable(f"{url}: malformed payload ({exc})") from exc

    return LiveState(host=host, listeners=listeners, complete=True)


def probe_live(
    host: str,
    ports: Iterable[int],
    connect: Callable[..., object] = socket.create_connection,
    timeout: float = 0.5,
) -> LiveState:
    """Candidate fallback: TCP-connect to each port. Not wired to any caller.

    Blind to loopback listeners and to ownership, so the result is marked
    incomplete -- and an incomplete `LiveState` is one the CLI refuses to grant
    on. Wiring it up would need that policy decided first.
    """
    listeners = []
    for port in ports:
        try:
            sock = connect((host, port), timeout=timeout)
        except OSError:
            continue
        close = getattr(sock, "close", None)
        if close is not None:
            close()
        listeners.append(Listener(addr=ANY_ADDR, port=port, container=None))

    return LiveState(host=host, listeners=tuple(listeners), complete=False)
