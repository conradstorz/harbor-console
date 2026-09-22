"""Every socket listening on this host.

Only the host itself can see loopback-bound listeners and non-Docker ones such
as sshd and tailscaled. An allocator blind to those would eventually hand one
out, which is why this is collected here and served to it rather than probed
from outside.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass

import psutil


class _Unavailable(tuple):
    """A distinguishable empty result: falsy, iterable, and identity-checkable."""


#: Returned when the socket table itself could not be read -- access denied,
#: a partly-readable `/proc`, anything. Distinguishable from "asked, and
#: nothing is listening": the page binds a socket of its own on every
#: successful cycle, so a genuinely empty reachable table never happens and
#: an empty result here always means collection failed.
LISTENING_UNAVAILABLE = _Unavailable()

#: A socket bound to IPv6 `::` accepts IPv4 traffic too, so it is the wildcard
#: in practice. The allocator's overlap rule knows `0.0.0.0` and nothing else,
#: so normalise here rather than teaching every consumer about both spellings.
IPV6_ANY = "::"
IPV4_ANY = "0.0.0.0"

#: The address that contends with every other on its host.
ANY_ADDR = IPV4_ANY

#: The two protocols a listening socket can speak. `proto` defaults to tcp
#: because tcp is all this collector gathered before UDP was added, so every
#: `Listener` built positionally by older code still means what it said.
PROTO_TCP = "tcp"
PROTO_UDP = "udp"


@dataclass(frozen=True)
class Listener:
    """One listening socket. `pid` is None when it belongs to another user."""

    addr: str
    port: int
    pid: int | None
    proto: str = PROTO_TCP


def listening_sockets(
    net_connections: Callable[..., object] = psutil.net_connections,
) -> tuple[Listener, ...]:
    """Collect listening TCP sockets and bound UDP sockets.

    Two distinct failure modes, degrading differently:

    - `net_connections` itself failing -- access denied, a partly-readable
      `/proc`, anything -- yields `LISTENING_UNAVAILABLE`. There is nothing
      to salvage, and it must not be mistaken for a host with nothing
      listening.
    - One malformed connection in an otherwise good list -- a missing
      attribute, a `laddr` that isn't psutil's named tuple, a port that
      won't `int()` -- is skipped. The rest of the list is still trustworthy
      and dropping the whole collection over one bad entry would be worse:
      the allocator would see a suspiciously empty host instead of a host
      missing one listener.
    """
    try:
        connections = net_connections(kind="inet")
    except Exception:
        return LISTENING_UNAVAILABLE

    found: set[Listener] = set()
    for connection in connections:  # type: ignore[union-attr]
        try:
            proto = PROTO_UDP if connection.type == socket.SOCK_DGRAM else PROTO_TCP
            if proto == PROTO_TCP:
                if connection.status != psutil.CONN_LISTEN:
                    continue
            elif connection.raddr:
                # UDP has no LISTEN state. A datagram socket with a peer is
                # a conversation, not a service waiting to be spoken to.
                continue
            laddr = connection.laddr
            if not laddr:
                continue
            addr = IPV4_ANY if laddr.ip == IPV6_ANY else laddr.ip
            found.add(
                Listener(addr=addr, port=int(laddr.port), pid=connection.pid, proto=proto)
            )
        except (AttributeError, TypeError, ValueError):
            continue

    return tuple(sorted(found, key=lambda item: (item.port, item.addr, item.proto)))


def addrs_overlap(a: str, b: str) -> bool:
    """True when two bind addresses contend for the same port.

    `0.0.0.0` claims every address on the host, so it overlaps anything. Two
    different specific addresses each hold the same port number without
    conflict -- which is how ARM held 100.69.239.123:49152 without claiming
    49152 from loopback.
    """
    if a == b:
        return True
    return ANY_ADDR in (a, b)
