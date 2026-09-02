"""Where the ledger and reality disagree.

Pure: leases, listeners and containers in, findings out. No I/O, so every rule
is testable with plain values -- the same reason `ports/allocate.py` is pure.

The join key is `(addr, port)`, compared by overlap, because that is the key the
ledger owns. Only the leases granted to the host being reconciled are read: the
ledger is fleet-wide on purpose, so another machine's lease is neither drift
here nor cover for a container running here.

The ledger carries no container name, so a port mismatch -- the claim that a
project moved -- is only made when the evidence supports it: no container on the
host publishes the leased address and port, *and* the container named for the
project publishes nothing that the project's own leases cover. A project whose
sidecar honours the second lease has moved nothing, and neither has one whose
sidecar is simply not running; both are reported honestly as their two halves.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from harbor_console.docker import DOCKER_UNAVAILABLE, Container
from harbor_console.listening import Listener
from harbor_console.ports.keys import addrs_overlap
from harbor_console.ports.ledger import Lease
from harbor_console.snapshot import Drift

DECLARED_NOT_RUNNING = "declared-not-running"
RUNNING_NOT_DECLARED = "running-not-declared"
PORT_MISMATCH = "port-mismatch"


def _covers(pairs: Iterable[tuple[str, int]], addr: str, port: int) -> bool:
    """Return True when any `(addr, port)` in `pairs` contends with `addr:port`."""
    return any(
        other_port == port and addrs_overlap(other_addr, addr)
        for other_addr, other_port in pairs
    )


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
    published = [pair for container in containers for pair in container.published]

    findings: list[Drift] = []
    mismatched: set[str] = set()

    if docker_available:
        by_name = {container.name: container for container in containers}
        for lease in mine:
            container = by_name.get(lease.project)
            if container is None or not container.published:
                continue
            if _covers(published, lease.addr, lease.port):
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
            mismatched.add(lease.project)

    for lease in mine:
        if lease.project in mismatched:
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
            if container.name in mismatched:
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

    return tuple(findings)
