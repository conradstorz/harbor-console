"""Every socket listening on this host.

Only the host itself can see loopback-bound listeners and non-Docker ones such
as sshd and tailscaled. An allocator blind to those would eventually hand one
out, which is why this is collected here and served to it rather than probed
from outside.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import psutil

#: A socket bound to IPv6 `::` accepts IPv4 traffic too, so it is the wildcard
#: in practice. The allocator's overlap rule knows `0.0.0.0` and nothing else,
#: so normalise here rather than teaching every consumer about both spellings.
IPV6_ANY = "::"
IPV4_ANY = "0.0.0.0"


@dataclass(frozen=True)
class Listener:
    """One listening socket. `pid` is None when it belongs to another user."""

    addr: str
    port: int
    pid: int | None


def listening_sockets(
    net_connections: Callable[..., object] = psutil.net_connections,
) -> tuple[Listener, ...]:
    """Collect listening TCP sockets. Degrades to empty rather than raising."""
    try:
        connections = net_connections(kind="tcp")
    except (psutil.Error, OSError):
        return ()

    found: set[Listener] = set()
    for connection in connections:  # type: ignore[union-attr]
        if connection.status != psutil.CONN_LISTEN:
            continue
        laddr = connection.laddr
        if not laddr:
            continue
        addr = IPV4_ANY if laddr.ip == IPV6_ANY else laddr.ip
        found.add(Listener(addr=addr, port=int(laddr.port), pid=connection.pid))

    return tuple(sorted(found, key=lambda item: (item.port, item.addr)))
