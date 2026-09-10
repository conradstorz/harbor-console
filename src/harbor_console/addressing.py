"""Where a lease is reachable, and where to probe it.

Two callers in different layers have to agree on this, and neither may import
the other: `web.py` prints an address on the page, and `webapp.py` connects to
one from the prober. They disagreed once, and the page paid for it -- the
renderer showed the tailnet address while the prober still asked the hostname,
which resolves to the LAN address, where a tailnet-bound service is not
listening. Two healthy services were reported LISTENING rather than UP.

So the rule lives here instead, pure and shared, the way `ports/keys.py` serves
the ledger and the allocator and `snapshot.py` serves the prober and the
renderer. No I/O, no rendering, no collection.
"""

from __future__ import annotations

from collections.abc import Sequence

from harbor_console.ports.keys import ANY_ADDR, addrs_overlap
from harbor_console.ports.ledger import Lease
from harbor_console.serve import Proxy


def reachable_address(
    lease: Lease, served_host: str, tailnet_address: str | None
) -> str:
    """The address a reader can reach this lease on, for the page to print.

    `0.0.0.0` is a bind, not a destination: nobody can point a browser at it.
    When the tailnet address is known, that is the address such a service
    answers on, so it is the one worth printing.

    Two conditions guard the substitution, and both are the difference between
    a useful address and a wrong one. The lease must belong to the host being
    served -- the ledger is fleet-wide while the tailnet address is one
    machine's, so a lease on another host shown at this address would name the
    wrong machine. And the lease's address must overlap the tailnet address by
    `ports.keys.addrs_overlap`, the rule the rest of the project joins on: a
    wildcard lease overlaps and is substituted, a lease already recorded at
    that address matches exactly, and a loopback lease overlaps neither, so
    `127.0.0.1` stays `127.0.0.1`. A service bound to loopback genuinely is
    not on the tailnet, and an address claiming otherwise would advertise
    reachability the bind refuses.
    """
    if tailnet_address is None:
        return lease.addr
    if lease.host != served_host:
        return lease.addr
    if not addrs_overlap(lease.addr, tailnet_address):
        return lease.addr
    return tailnet_address


def probe_target(lease: Lease, served_host: str, tailnet_address: str | None) -> str:
    """The host or address to connect to when probing this lease.

    Mostly `reachable_address`, with two differences that come from probing
    being done by a process on the host rather than by a reader elsewhere.

    A lease on another machine is probed by its hostname: its address is not
    recorded anywhere this process can see, and the name is the only handle it
    has. A loopback lease, unreachable from the tailnet, is perfectly
    reachable from here -- probing it at `127.0.0.1` is the only way to learn
    anything about it at all, which is why the page may show `127.0.0.1` while
    the probe still succeeds.

    And the result is never a wildcard. `reachable_address` may return
    `0.0.0.0` when no tailnet address is known, which is fine to print but not
    somewhere to connect; the probe falls back to the hostname it used before.
    """
    if lease.host != served_host:
        return lease.host
    address = reachable_address(lease, served_host, tailnet_address)
    return lease.host if address == ANY_ADDR else address


def url_host(addr: str) -> str:
    """`addr` as a URL host: IPv6 literals need brackets, nothing else does.

    Every URL this project builds interpolates `http://{host}:{port}`, and an
    IPv6 literal spliced in bare turns its own colons into a port separator.
    Hostnames and IPv4 pass through untouched, as does an already-bracketed
    literal.
    """
    if ":" in addr and not addr.startswith("["):
        return f"[{addr}]"
    return addr


def fronts_for(
    lease: Lease, served_host: str, proxies: Sequence[Proxy]
) -> tuple[Proxy, ...]:
    """Every `tailscale serve` front that proxies to this lease.

    The leased address is where a service binds, which is not always where a
    reader can use it: GTE sets Secure cookies, so a login over the plain
    leased port never completes and the front's HTTPS URL is the only address
    that works. The page shows both -- the lease is the ledger's fact, the
    front is the way in.

    Matched on the backend by `ports.keys.addrs_overlap`, the rule the rest of
    the project joins on, so serve's `127.0.0.1:8080` answers a lease recorded
    as `0.0.0.0:8080`. A lease on another host is never matched: the front is
    this machine's, and another machine's lease on the same port is not what
    it proxies. A front parsed without its host is dropped, because it cannot
    be turned into a link and a row saying "reachable at" with no address is
    worse than silence.
    """
    if lease.host != served_host:
        return ()
    return tuple(
        sorted(
            (
                proxy
                for proxy in proxies
                if proxy.url is not None
                and proxy.backend_port == lease.port
                and addrs_overlap(proxy.backend_addr, lease.addr)
            ),
            key=lambda proxy: (proxy.port, proxy.path),
        )
    )
