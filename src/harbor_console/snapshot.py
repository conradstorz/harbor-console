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
    """Everything the page shows, collected at one moment.

    `frozen=True` here is shallow: it stops a field being rebound, but `metrics`
    and `health` are ordinary dicts whose contents can still be mutated, and a
    snapshot is unhashable, so there is no `set[Snapshot]`. That is enough for
    the pattern this serves -- the prober publishes one, handlers only read it
    -- but a handler that mutates `metrics` in place edits what every other
    reader sees; build a new snapshot instead.

    `probed` separates "collected, found nothing" from "collected nothing
    yet". It defaults to False because that is the honest default: a
    snapshot nobody has filled in must not read as a clean bill of health
    for a fleet that has never been looked at.
    """

    collected: datetime
    metrics: dict[str, str | float | int]
    #: When the ledger file this snapshot's leases came from was last written
    #: to disk, or None when that could not be determined -- a missing or
    #: unreadable ledger, or a snapshot collected before the first cycle. The
    #: server never writes `services.toml`; only `ports sync`, run on the dev
    #: box, does, and `install.sh` is what carries a fresh copy here. This is
    #: how a copy stale because `install.sh` was forgotten becomes visible on
    #: the page instead of silently degrading its directory and drift section.
    ledger_written: datetime | None = None
    leases: tuple[Lease, ...] = ()
    listeners: tuple[Listener, ...] = ()
    containers: tuple[Container, ...] = ()
    docker_available: bool = True
    health: dict[tuple[str, str], Health] = field(default_factory=dict)
    drift: tuple[Drift, ...] = ()
    #: Why the last collection cycle failed, whatever its source -- the
    #: ledger, a collector, the prober or the reconciler. Naming it for the
    #: ledger alone pointed every failure at `services.toml`.
    collection_error: str | None = None
    #: True once a collection cycle has completed. The starting snapshot,
    #: which collects nothing, leaves it False so the page can say "not yet"
    #: rather than assert a state it has never looked at.
    probed: bool = False
