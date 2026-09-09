"""What `tailscale serve` fronts, and the true port behind it.

A serve proxy is the one thing on this host that holds a tailnet port without
being a container and without being anybody's lease: `tailscaled` binds it, and
forwards to a backend that usually *is* declared. Port 8443 on hpz440 fronts
`127.0.0.1:8080`, which is gte's leased console -- so the port looks
undeclared, is undeclared, and is nonetheless serving a declared service.

Reporting the front alone would send an operator hunting for a process that
does not exist. This collector supplies the other half, so `reconcile` can name
the backend and whatever declares it.

Degrades quietly, like every collector but `tailnet.py`: no tailscale, a bad
exit, unreadable JSON and a hang all yield no proxies. Absence is reported as
*absence of knowledge* by `reconcile`, never as "nothing is proxying it" --
that claim would be false in exactly the case this collector failed.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

#: A bound on `tailscale serve status --json`. This runs in the prober thread
#: on every cycle, like `docker ps`, so the reasoning in `DOCKER_TIMEOUT_SECONDS`
#: applies unchanged: a wedged binary with no bound freezes the last good
#: snapshot in place while it is still served as current.
SERVE_TIMEOUT_SECONDS = 2.0

#: Names the ledger never records. It records addresses, so a backend written
#: as `localhost` has to be resolved to the address a lease could match.
_LOOPBACK_NAMES = ("localhost", "localhost.localdomain")


@dataclass(frozen=True)
class Proxy:
    """One `tailscale serve` front and the backend it forwards to."""

    port: int
    path: str
    backend_addr: str
    backend_port: int


def serve_proxies(
    run: Callable[..., object] = subprocess.run,
    timeout: float = SERVE_TIMEOUT_SECONDS,
) -> tuple[Proxy, ...]:
    """Collect every HTTP(S) front `tailscale serve` is running."""
    try:
        result = run(
            ["tailscale", "serve", "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ()
    except (FileNotFoundError, OSError):
        return ()

    if result.returncode != 0:  # type: ignore[attr-defined]
        return ()

    try:
        status = json.loads(result.stdout or "{}")  # type: ignore[attr-defined]
    except (ValueError, TypeError):
        return ()
    if not isinstance(status, dict):
        return ()

    proxies: list[Proxy] = []
    for front, config in (status.get("Web") or {}).items():
        port = _front_port(front)
        if port is None:
            continue
        handlers = (config or {}).get("Handlers") or {}
        for path, handler in handlers.items():
            backend = _backend((handler or {}).get("Proxy"))
            if backend is None:
                continue
            addr, backend_port = backend
            proxies.append(Proxy(port, str(path), addr, backend_port))

    return tuple(sorted(proxies, key=lambda item: (item.port, item.path)))


def _front_port(front: str) -> int | None:
    """The port from a `host:port` key, or None when there is not one.

    Rsplit, because the host half is a DNS name here rather than an address --
    but a key with no colon at all is not something to guess a port for.
    """
    _, sep, port = str(front).rpartition(":")
    if not sep:
        return None
    try:
        return int(port)
    except ValueError:
        return None


def _backend(proxy: object) -> tuple[str, int] | None:
    """The `(addr, port)` a proxy target names, or None.

    Parsed rather than pattern-matched: the scheme can be `https+insecure`,
    which is what `serve` writes for a backend with a self-signed certificate,
    and matching on `http://` would miss it.

    A target with no explicit port is skipped. The point of this collector is
    to name the true port; inferring 80 or 443 from the scheme would be a
    guess printed as a fact, and `serve` writes the port in practice.
    """
    if not isinstance(proxy, str) or not proxy:
        return None
    try:
        parts = urlsplit(proxy)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host or port is None:
        return None
    if host in _LOOPBACK_NAMES:
        host = "127.0.0.1"
    return host, port
