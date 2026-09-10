"""Where the ledger and reality disagree.

Pure: leases, listeners and containers in, findings out. No I/O, so every rule
is testable with plain values -- the same reason `ports/allocate.py` is pure.

The join key is `(addr, port)`, compared by overlap, because that is the key the
ledger owns. Only the leases granted to the host being reconciled are read: the
ledger is fleet-wide on purpose, so another machine's lease is neither drift
here nor cover for a container running here.

The ledger carries no container name, so a port mismatch -- the claim that a
project moved -- is only made when the evidence supports it: nothing covering
the leased address and port is running, *and* the container named for the
project publishes nothing that the project's own leases cover. A project whose
sidecar honours the second lease has moved nothing, and neither has one whose
sidecar is simply not running.

Cover is not pooled across projects. A container covers a lease when it is the
project's own container, or when its name is no project's name at all -- a
sidecar like `gte-metrics`. Another *project's* container never covers, because
if it did, two projects that had swapped ports would answer each other's leases
and the page would go clean on exactly the collision this exists to catch.

Withholding a mismatch usually leaves the honest two halves, but not always: if
a listener already covers the lease, the `declared-not-running` half is
suppressed by the listener rule and nothing is reported. That case is not
collision-class -- something *is* serving the leased port, and a non-Docker
service satisfying a lease is a state this module accepts -- so it is left as
it is rather than forced into a finding.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence, Set as AbstractSet

from harbor_console.docker import DOCKER_UNAVAILABLE, Container
from harbor_console.listening import Listener
from harbor_console.ports.keys import addrs_overlap
from harbor_console.ports.ledger import Lease
from harbor_console.serve import Proxy
from harbor_console.snapshot import Drift

DECLARED_NOT_RUNNING = "declared-not-running"
RUNNING_NOT_DECLARED = "running-not-declared"
PORT_MISMATCH = "port-mismatch"
UNDECLARED_TAILNET_LISTENER = "undeclared-tailnet-listener"

#: Linux's default local port range: what the kernel hands out when nobody
#: chose a port. An undeclared listener in here is not the deliberate act
#: `undeclared-tailnet-listener` exists to catch -- tailscaled's own peerapi
#: lands in it, on a different port after every restart -- and a standing
#: false finding that changes shape daily teaches operators to ignore the
#: drift section. Leases inside the range are unaffected: they are covered
#: before this test is reached, which is how ARM's 49152 stays reconciled.
EPHEMERAL_MIN = 32768
EPHEMERAL_MAX = 60999


def _covers(pairs: Iterable[tuple[str, int]], addr: str, port: int) -> bool:
    """Return True when any `(addr, port)` in `pairs` contends with `addr:port`."""
    return any(
        other_port == port and addrs_overlap(other_addr, addr)
        for other_addr, other_port in pairs
    )


def _cover_for(
    containers: Sequence[Container], project: str, projects: AbstractSet[str]
) -> list[tuple[str, int]]:
    """Return the published ports allowed to answer `project`'s leases.

    A container covers when it is the project's own, or when its name is no
    leased project's name -- a sidecar such as `gte-metrics`, which serves a
    port on the project's behalf. A container named for a *different* project
    is excluded: pooling every container's ports lets two projects that have
    swapped ports satisfy each other's leases, and the swap disappears.

    `projects` is fleet-wide, not just this host's: a container named for a
    project whose leases all live on another host is still that project's
    container, not an anonymous sidecar, so it must not cover a local lease
    either. It is taken as an already-built set so this function need not
    copy it on every call.
    """
    return [
        pair
        for container in containers
        if container.name == project or container.name not in projects
        for pair in container.published
    ]


def _lease_order(lease: Lease) -> tuple[str, int, str, str]:
    """Order leases totally, so two sharing a project and port cannot tie.

    Without `addr` and `name` a tie falls back to the order the ledger happened
    to be read in, and the page reshuffles between refreshes for no reason.
    """
    return (lease.project, lease.port, lease.addr, lease.name)


def find_drift(
    leases: Sequence[Lease],
    listeners: Sequence[Listener],
    containers: Sequence[Container],
    host: str,
    tailnet_address: str | None = None,
    proxies: Sequence[Proxy] = (),
) -> tuple[Drift, ...]:
    """Name every disagreement between the ledger and `host`.

    Leases granted to another host are ignored entirely, so the module is
    correct on its own rather than depending on a caller to pre-filter.

    Whether Docker could be asked is read from `containers` itself --
    `DOCKER_UNAVAILABLE` -- rather than taken as a separate flag that could
    disagree with it. When Docker is unavailable, every finding that needs
    container evidence is withheld; nothing may claim a port is undeclared.
    """
    docker_available = containers is not DOCKER_UNAVAILABLE
    mine = sorted((lease for lease in leases if lease.host == host), key=_lease_order)
    leased = {(lease.addr, lease.port) for lease in mine}
    bound = [(listener.addr, listener.port) for listener in listeners]
    # Fleet-wide, not `mine`: a container named for a project whose leases all
    # live on another host is still a named project's container, not an
    # anonymous sidecar, so it must not cover a lease on this host either.
    projects = frozenset(lease.project for lease in leases)

    findings: list[Drift] = []
    # Two separate records, because the mismatch decision is per lease: one
    # lease of a project may be reported while a sibling lease of the same
    # project must still go through the liveness loop. A single set keyed by
    # project would mute the sibling and hide a dead port entirely.
    mismatched_leases: set[tuple[str, int, str, str]] = set()
    mismatched_containers: set[str] = set()

    if docker_available:
        by_name = {container.name: container for container in containers}
        # Every lease of the same project shares one cover list; a project
        # with several leases (a sibling-container shape) would otherwise
        # rebuild it once per lease for no new evidence.
        cover_by_project: dict[str, list[tuple[str, int]]] = {}
        for lease in mine:
            container = by_name.get(lease.project)
            if container is None or not container.published:
                continue
            cover = cover_by_project.get(lease.project)
            if cover is None:
                cover = _cover_for(containers, lease.project, projects)
                cover_by_project[lease.project] = cover
            if _covers(cover, lease.addr, lease.port):
                continue
            own = {
                (other.addr, other.port)
                for other in mine
                if other.project == lease.project
            }
            if any(_covers(own, addr, port) for addr, port in container.published):
                continue
            actual = ", ".join(f"{a}:{p}" for a, p in sorted(container.published))
            findings.append(
                Drift(
                    PORT_MISMATCH,
                    f"{lease.project} is leased {lease.addr}:{lease.port} "
                    f"but container '{container.name}' publishes {actual}",
                )
            )
            mismatched_leases.add(_lease_order(lease))
            mismatched_containers.add(container.name)

    for lease in mine:
        if _lease_order(lease) in mismatched_leases:
            continue
        if not _covers(bound, lease.addr, lease.port):
            findings.append(
                Drift(
                    DECLARED_NOT_RUNNING,
                    f"{lease.project} leases {lease.addr}:{lease.port} as "
                    f"{lease.name}, nothing is listening",
                )
            )

    if docker_available:
        for container in sorted(containers, key=lambda item: item.name):
            if container.name in mismatched_containers:
                continue
            for addr, port in sorted(container.published):
                if not _covers(leased, addr, port):
                    findings.append(
                        Drift(
                            RUNNING_NOT_DECLARED,
                            f"container '{container.name}' publishes {addr}:{port}, "
                            "which no lease covers",
                        )
                    )

        findings.extend(
            _undeclared_tailnet_listeners(
                listeners, containers, leased, mine, tailnet_address, proxies
            )
        )

    return tuple(findings)


def _undeclared_tailnet_listeners(
    listeners: Sequence[Listener],
    containers: Sequence[Container],
    leased: AbstractSet[tuple[str, int]],
    mine: Sequence[Lease],
    tailnet_address: str | None,
    proxies: Sequence[Proxy],
) -> list[Drift]:
    """Report a host process holding a tailnet port that nothing declares.

    The gap this closes: `running-not-declared` walks containers, so a process
    outside Docker binding a tailnet port produced no finding at all -- no
    lease to list it in the directory, and no container to catch it in drift.
    `tailscale serve` held 8443 that way, invisibly.

    Only an *exact* bind to the tailnet address qualifies. A wildcard listener
    is every daemon on the box -- sshd, resolved, whatever a developer left
    running -- and flagging those would bury this finding in noise the ledger
    was never meant to govern; loopback is not published to the tailnet at
    all. Binding the tailnet address specifically is a deliberate act of
    publishing to the tailnet, which is exactly what the ledger governs.

    Withheld entirely without container evidence (the caller runs this only
    when Docker could be read) or without a known tailnet address, for the
    same reason `running-not-declared` is: neither absence can distinguish an
    undeclared listener from one that is perfectly well accounted for.

    Also withheld when every `tailscale serve` proxy on the port forwards to
    a backend some local lease covers -- the front is then fully accounted
    for by that lease's directory row -- unless no proxy is known at all, in
    which case absence of serve knowledge must not read as "accounted for".
    """
    if tailnet_address is None:
        return []

    published = [pair for container in containers for pair in container.published]
    findings = []
    for addr, port in sorted(
        {
            (listener.addr, listener.port)
            for listener in listeners
            if listener.addr == tailnet_address
        }
    ):
        if _covers(leased, addr, port):
            continue
        # Already reported against the container that publishes it. The same
        # port under two names reads as two problems.
        if _covers(published, addr, port):
            continue
        # A front every one of whose backends is leased is fully accounted
        # for: somebody configured serve to publish a declared service, and
        # the directory row for that lease already leads with the front's
        # URL. A standing finding here would never clear on a healthy,
        # fully-adopted host, which teaches operators to skip the one
        # section that exists to be read. A front to an *unleased* backend
        # still reports, and absent serve knowledge nothing is suppressed --
        # absence of knowledge must not read as "accounted for".
        behind = [proxy for proxy in proxies if proxy.port == port]
        if behind and all(
            any(
                lease.port == proxy.backend_port
                and addrs_overlap(lease.addr, proxy.backend_addr)
                for lease in mine
            )
            for proxy in behind
        ):
            continue
        clause = _proxy_clause(port, proxies, mine, containers)
        # A kernel-assigned port nobody chose is not a port anybody published.
        # A serve front is the exception: somebody configured it, and it
        # survives a restart, so the intent is evidenced wherever it landed.
        if not clause and EPHEMERAL_MIN <= port <= EPHEMERAL_MAX:
            continue
        findings.append(
            Drift(
                UNDECLARED_TAILNET_LISTENER,
                f"{addr}:{port} is listening on the tailnet, and no lease "
                "covers it" + clause,
            )
        )

    return findings


def _proxy_clause(
    port: int,
    proxies: Sequence[Proxy],
    mine: Sequence[Lease],
    containers: Sequence[Container],
) -> str:
    """Name the true port behind a `tailscale serve` front, and what runs there.

    Says nothing at all when no proxy is known for the port. `serve_proxies`
    degrades to an empty tuple when tailscale could not be asked, so "nothing
    is proxying it" would be a false claim in exactly the case the collector
    failed -- and an operator told a port is unexplained goes looking for a
    process, which is the wrong hunt when a proxy is fronting a service that
    is declared and healthy.
    """
    behind = [proxy for proxy in proxies if proxy.port == port]
    if not behind:
        return ""

    parts = []
    for proxy in behind:
        target = f"{proxy.backend_addr}:{proxy.backend_port}"
        parts.append(
            f"{proxy.path} to {target}, "
            f"{_owner_of(proxy.backend_addr, proxy.backend_port, mine, containers)}"
        )
    return " -- tailscale serve proxies " + "; ".join(parts)


def _owner_of(
    addr: str,
    port: int,
    mine: Sequence[Lease],
    containers: Sequence[Container],
) -> str:
    """What declares or runs a backend: a lease first, then a container.

    The lease is the better answer -- it names the project the ledger knows --
    but a backend with no lease may still be running code somebody can find,
    which is the question an operator actually has.
    """
    for lease in mine:
        if lease.port == port and addrs_overlap(lease.addr, addr):
            return f"which {lease.project} leases as {lease.name}"
    for container in containers:
        if _covers(container.published, addr, port):
            return f"published by container '{container.name}'"
    return "which nothing declares"
