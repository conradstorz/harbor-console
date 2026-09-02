"""Where the ledger and reality disagree.

Pure: leases, listeners and containers in, findings out. No I/O, so every rule
is testable with plain values -- the same reason `ports/allocate.py` is pure.

The join key is `(addr, port)`, compared by overlap, because that is the key the
ledger owns. The ledger carries no container name, so a port mismatch is
reported only when a container's name equals a lease's project name; an
unmatched pair is reported honestly as its two halves instead.
"""

from __future__ import annotations

from collections.abc import Sequence

from harbor_console.docker import Container
from harbor_console.listening import Listener
from harbor_console.ports.keys import addrs_overlap
from harbor_console.ports.ledger import Lease
from harbor_console.snapshot import Drift

DECLARED_NOT_RUNNING = "declared-not-running"
RUNNING_NOT_DECLARED = "running-not-declared"
PORT_MISMATCH = "port-mismatch"


def find_drift(
    leases: Sequence[Lease],
    listeners: Sequence[Listener],
    containers: Sequence[Container],
    docker_available: bool,
) -> tuple[Drift, ...]:
    """Name every disagreement between the ledger and the host."""
    findings: list[Drift] = []
    leased = {(lease.addr, lease.port) for lease in leases}
    mismatched: set[str] = set()

    if docker_available:
        by_name = {container.name: container for container in containers}
        for lease in sorted(leases, key=lambda item: (item.project, item.port)):
            container = by_name.get(lease.project)
            if container is None or not container.published:
                continue
            if not any(
                published_port == lease.port and addrs_overlap(published_addr, lease.addr)
                for published_addr, published_port in container.published
            ):
                actual = ", ".join(f"{a}:{p}" for a, p in container.published)
                findings.append(
                    Drift(
                        PORT_MISMATCH,
                        f"{lease.project} is leased {lease.addr}:{lease.port} "
                        f"but container '{container.name}' publishes {actual}",
                    )
                )
                mismatched.add(lease.project)

    for lease in sorted(leases, key=lambda item: (item.project, item.port)):
        if lease.project in mismatched:
            continue
        if not any(
            listener.port == lease.port and addrs_overlap(listener.addr, lease.addr)
            for listener in listeners
        ):
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
            for addr, port in container.published:
                if not any(
                    addrs_overlap(addr, leased_addr) and port == leased_port
                    for leased_addr, leased_port in leased
                ):
                    findings.append(
                        Drift(
                            RUNNING_NOT_DECLARED,
                            f"container '{container.name}' publishes {addr}:{port}, "
                            "which no lease covers",
                        )
                    )

    return tuple(findings)
