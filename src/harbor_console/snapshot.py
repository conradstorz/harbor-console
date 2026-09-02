"""The contract between the prober and the renderer.

Data only. The prober publishes one of these on an interval; the handler reads
the last one and renders it. Keeping it in its own module lets both sides
import it without a cycle, the way `ports/keys.py` serves the allocator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from harbor_console.docker import Container
from harbor_console.listening import Listener
from harbor_console.ports.ledger import Lease
from harbor_console.probe import Health


@dataclass(frozen=True)
class Drift:
    """One way the ledger and reality disagree."""

    kind: str
    detail: str


@dataclass(frozen=True)
class Snapshot:
    """Everything the page shows, collected at one moment."""

    collected: datetime
    metrics: dict[str, str | float | int]
    leases: tuple[Lease, ...] = ()
    listeners: tuple[Listener, ...] = ()
    containers: tuple[Container, ...] = ()
    docker_available: bool = True
    health: dict[tuple[str, str], Health] = field(default_factory=dict)
    drift: tuple[Drift, ...] = ()
    ledger_error: str | None = None
