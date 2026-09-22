"""Every listening socket, classified by who can reach it and what accounts for it.

Pure: listeners, containers and the host's tailnet address in; one entry per
socket out. The page's four findings answer "where do the declarations and the
host disagree"; this answers the prior question, "what is listening at all",
which is the one a rule cannot be trusted with -- a filter applied before
anyone can look is unfalsifiable by inspection.

Two deliberate limitations:

- **Attribution ignores protocol.** `docker.py` drops the protocol from
  `NetworkSettings.Ports`, so `Container.published` is `(addr, port)` only. A
  UDP listener therefore attributes to a container publishing the same TCP
  port. The entry still reports the true protocol, and threading protocol
  through `published` would ripple into `directory.py` for a case that does
  not occur on this host.
- **Most PIDs are unreadable.** The service runs unprivileged, and psutil
  cannot map socket inode to PID for another user's socket, so sshd,
  tailscaled and dockerd arrive with `pid=None`. The inventory is complete;
  the attribution is partial, and an unattributed row is the signal rather
  than a defect.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from ipaddress import ip_address

from harbor_console.docker import DOCKER_UNAVAILABLE, Container
from harbor_console.listening import ANY_ADDR, IPV6_ANY, Listener, addrs_overlap

#: Who can reach a socket, given the address it is bound to.
REACH_LOOPBACK = "loopback"
REACH_TAILNET = "tailnet"
REACH_LAN = "LAN"
REACH_ANY = "LAN + tailnet"

#: Tailscale's fixed ULA allocation. `tailnet.py` runs `tailscale ip -4` and
#: never learns this host's IPv6 tailnet address, so without matching the
#: prefix the tailnet's own IPv6 listeners would be labelled LAN. The prefix
#: is a documented constant of Tailscale's, not a fact about this host.
TAILNET_ULA_PREFIX = "fd7a:115c:a1e0:"

#: Attribution when Docker could not be read: the truth is that we do not
#: know, which is not the same as nothing accounting for the socket.
UNKNOWN = "unknown"

#: Attribution for the page's own bind -- a host process no container
#: publishes, and the one such listener that is declared by being this program.
OWN_NAME = "harbor-console-web"


@dataclass(frozen=True)
class Entry:
    """One listening socket, with who can reach it and what accounts for it."""

    proto: str
    addr: str
    port: int
    reach: str
    #: A container name, `OWN_NAME`, "pid <n>", `UNKNOWN`, or "" for a socket
    #: nothing accounts for.
    accounted: str


def reach_of(addr: str, tailnet_address: str | None) -> str:
    """Who can reach a socket bound to `addr`.

    Link-local `fe80::/10` counts as LAN: a DHCPv6 client socket is reachable
    from the link, which is the LAN, and a fifth class for it would be
    precision nobody reads. A scoped address (`fe80::1%eno1`) parses normally
    and lands there too, like any other specific address; an address that
    genuinely will not parse also lands in LAN.
    """
    if addr in (ANY_ADDR, IPV6_ANY):
        return REACH_ANY
    if tailnet_address is not None and addr == tailnet_address:
        return REACH_TAILNET
    if addr.lower().startswith(TAILNET_ULA_PREFIX):
        return REACH_TAILNET
    try:
        if ip_address(addr).is_loopback:
            return REACH_LOOPBACK
    except ValueError:
        return REACH_LAN
    return REACH_LAN


def _accounted(
    listener: Listener,
    containers: Sequence[Container],
    tailnet_address: str | None,
    own_port: int | None,
    docker_available: bool,
) -> str:
    """What accounts for this socket, most informative answer first."""
    if (
        own_port is not None
        and listener.port == own_port
        and tailnet_address is not None
        and listener.addr == tailnet_address
    ):
        return OWN_NAME
    if docker_available:
        for container in sorted(containers, key=lambda item: item.name):
            for addr, port in container.published:
                if port == listener.port and addrs_overlap(addr, listener.addr):
                    return container.name
    if listener.pid is not None:
        return f"pid {listener.pid}"
    return "" if docker_available else UNKNOWN


def build_inventory(
    listeners: Sequence[Listener],
    containers: Sequence[Container],
    tailnet_address: str | None,
    own_port: int | None = None,
) -> tuple[Entry, ...]:
    """One entry per listening socket, ordered as the collector orders them."""
    docker_available = containers is not DOCKER_UNAVAILABLE
    entries = [
        Entry(
            proto=listener.proto,
            addr=listener.addr,
            port=listener.port,
            reach=reach_of(listener.addr, tailnet_address),
            accounted=_accounted(
                listener, containers, tailnet_address, own_port, docker_available
            ),
        )
        for listener in listeners
    ]
    return tuple(sorted(entries, key=lambda item: (item.port, item.addr, item.proto)))
