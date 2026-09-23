"""The contract between the prober and the renderer.

Data only. The prober publishes one of these on an interval; the handler reads
the last one and renders it. Its own module so neither side imports the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from harbor_console.directory import Finding, Row
from harbor_console.docker import Container
from harbor_console.inventory import Entry
from harbor_console.probe import Health
from harbor_console.storage import StorageEntry


@dataclass(frozen=True)
class Snapshot:
    """Everything the page shows, collected at one moment.

    `frozen=True` is shallow: `metrics` and `health` are ordinary dicts. The
    prober publishes one, handlers only read it; build a new snapshot rather
    than editing one in place.

    `probed` separates "collected, found nothing" from "collected nothing
    yet". It defaults to False because that is the honest default.
    """

    collected: datetime
    metrics: dict[str, str | float | int]
    rows: tuple[Row, ...] = ()
    findings: tuple[Finding, ...] = ()
    #: One entry per listening socket: what it is, who can reach it, and what
    #: accounts for it. Derived from the listeners collector's own result and
    #: `containers` so the renderer does no policy of its own.
    inventory: tuple[Entry, ...] = ()
    containers: tuple[Container, ...] = ()
    docker_available: bool = True
    traefik_available: bool = True
    listeners_available: bool = True
    #: Keyed by `Row.name` -- the router name for HTTP rows.
    health: dict[str, Health] = field(default_factory=dict)
    #: Why the last collection cycle failed, whatever its source.
    collection_error: str | None = None
    probed: bool = False
    #: The tailnet address this process bound. None only outside `webapp.main`.
    tailnet_address: str | None = None
    #: Every filesystem, volume-group slack, stray device and named-but-
    #: unmeasured network mount. Empty until the first cycle runs, which
    #: `probed` is what distinguishes.
    storage: tuple[StorageEntry, ...] = ()
