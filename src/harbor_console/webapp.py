"""The harbor-console-web entry point: one prober thread and one HTTP server.

The two processes of this project have independent lifetimes. Logging in at the
attached monitor must not take this page down, and this page must not depend on
anyone being logged in.

Three refusals, all at startup, all deliberate. Without a Tailscale address,
without a readable ledger, or without a lease of its own, this process exits
non-zero rather than binding something broader or serving something it cannot
vouch for; systemd restarts it, and an operator reads the reason in journald.
Binding *is* the access control -- the page is an inventory of every service on
the host -- so there is no fallback address, no `--host`, and no dev mode
(ADR 7).

After that, nothing takes the page down. The prober thread collects on an
interval and publishes one snapshot; a cycle that fails leaves the last good
snapshot standing with the reason attached, because a page whose whole job is
telling you something is wrong must not become the thing that is wrong.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

from harbor_console.docker import DOCKER_UNAVAILABLE, Container, running_containers
from harbor_console.listening import Listener, listening_sockets
from harbor_console.ports.ledger import Lease, LedgerError, load_leases
from harbor_console.probe import Health, probe
from harbor_console.reconcile import find_drift
from harbor_console.snapshot import Snapshot
from harbor_console.system import collect_system_metrics
from harbor_console.tailnet import TailnetUnavailable, tailscale_address
from harbor_console.web import make_handler

#: This service is declared in the same ledger it serves, and takes its port
#: from there. Every service is declared, including this one.
WEB_PROJECT = "harbor-console"
WEB_PORT_NAME = "web"

PROBE_INTERVAL_SECONDS = 30.0

#: The ledger `harbor-console ports sync` writes, resolved the same way
#: `ports/cli.py` resolves it: relative to this file, not to a working
#: directory a systemd unit does not control.
LEDGER_PATH = Path(__file__).resolve().parents[2] / "services.toml"

EXIT_OK = 0
EXIT_REFUSED = 1


class NotDeclared(Exception):
    """The ledger carries no lease for this service."""


class SnapshotHolder:
    """The one piece of shared state: the last snapshot the prober published.

    One writer (the prober) and any number of readers (request threads). The
    lock only guards the rebinding of the reference; a `Snapshot` is never
    edited in place, so a reader that has one holds a consistent whole even
    while the next cycle publishes its successor.
    """

    def __init__(self, initial: Snapshot) -> None:
        self._lock = threading.Lock()
        self._snapshot = initial

    def get(self) -> Snapshot:
        """Return the last published snapshot."""
        with self._lock:
            return self._snapshot

    def set(self, snapshot: Snapshot) -> None:
        """Publish a snapshot, replacing the previous one."""
        with self._lock:
            self._snapshot = snapshot


def own_lease(leases: Iterable[Lease]) -> Lease:
    """Find this service's own lease in the ledger it serves.

    A missing lease is a refusal, not a default port: a page bound to a port
    no lease reserves is exactly the collision the ledger exists to prevent,
    and this service gets no exemption from its own rule.
    """
    for lease in leases:
        if lease.project == WEB_PROJECT and lease.name == WEB_PORT_NAME:
            return lease
    raise NotDeclared(
        f"no lease for {WEB_PROJECT}/{WEB_PORT_NAME}; this service must be declared"
    )


def own_port(leases: Iterable[Lease]) -> int:
    """Return this service's own leased port."""
    return own_lease(leases).port


def starting_snapshot(
    host: str, leases: tuple[Lease, ...], now: datetime
) -> Snapshot:
    """The page's first answer, standing only until the prober's first cycle.

    Collects nothing: the server binds before any collector runs, so a request
    landing in that window gets a page rather than a traceback, and a slow or
    hung collector can never delay the bind.
    """
    return Snapshot(
        collected=now,
        metrics={
            "hostname": host,
            "uptime": "collecting",
            "cpu_utilization": 0.0,
            "memory_utilization": 0.0,
            "disk_utilization": 0.0,
            "ipv4_address": "collecting",
            "docker_container_count": 0,
            "current_datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        },
        leases=leases,
    )


def collect_snapshot(
    leases: Iterable[Lease],
    now: datetime,
    collector: Callable[[], dict[str, str | float | int]] = collect_system_metrics,
    listeners: Callable[[], tuple[Listener, ...]] = listening_sockets,
    containers: Callable[[], tuple[Container, ...]] = running_containers,
    prober: Callable[[str, int], Health] = probe,
) -> Snapshot:
    """Gather every source once and fold it into one snapshot.

    `leases` is materialised before use: `find_drift` walks it more than once,
    so a generator would silently hand it an empty second pass.

    Whether Docker could be read is the identity of `DOCKER_UNAVAILABLE`, so
    the sentinel is passed on to `find_drift` intact and only flattened into
    the snapshot, where the renderer wants a plain tuple and a flag.

    The host reconciled against is the one this page serves, read from the
    metrics -- the same field `web.ports_payload` publishes as `host`. A lease
    granted to another machine is therefore neither drift here nor cover for
    something running here.
    """
    held = tuple(leases)
    metrics = collector()
    host = str(metrics["hostname"])
    found = listeners()
    running = containers()

    health = {
        (lease.project, lease.name): prober(lease.host, lease.port) for lease in held
    }

    return Snapshot(
        collected=now,
        metrics=metrics,
        leases=held,
        listeners=found,
        containers=tuple(running),
        docker_available=running is not DOCKER_UNAVAILABLE,
        health=health,
        drift=find_drift(held, found, running, host),
        ledger_error=None,
    )


def probe_loop(
    holder: SnapshotHolder,
    collect: Callable[[], Snapshot],
    sleep: Callable[[float], None] = time.sleep,
    interval: float = PROBE_INTERVAL_SECONDS,
) -> None:
    """Publish a fresh snapshot on an interval until interrupted.

    A collection failure never takes the page down: the last good snapshot
    stands, with the reason attached, because a page that exists to tell you
    something is wrong must not become the thing that is wrong. The catch is
    broad on purpose -- an unreadable ledger, a psutil that raised, a probe
    that found a new way to fail -- since every one of those has the same
    right answer, and there is no operator watching this thread. A successful
    cycle publishes `ledger_error=None`, so a reason never outlives its cause.
    """
    while True:
        try:
            holder.set(collect())
        except Exception as exc:  # noqa: BLE001 - a supervisor loop, see above
            holder.set(replace(holder.get(), ledger_error=_reason(exc)))
        try:
            sleep(interval)
        except KeyboardInterrupt:
            return


def _reason(exc: Exception) -> str:
    """Describe a failed cycle, even when the exception carries no message."""
    text = str(exc)
    return text if text else exc.__class__.__name__


def main(
    argv: list[str] | None = None,
    server_factory: Callable[..., object] = ThreadingHTTPServer,
    start_prober: Callable[[SnapshotHolder], None] | None = None,
) -> int:
    """Entry point. Refuses to start rather than binding anything broader.

    `argv` is accepted and ignored: there are deliberately no options. A
    `--host` would be the one flag that could publish this page to the LAN,
    so there is nothing for a command line to say.

    The server is a `ThreadingHTTPServer` because `web.make_handler` speaks
    HTTP/1.1: with a single-threaded server one idle keep-alive client would
    hold the only handler and the page would stop answering everyone else --
    the same failure the background prober exists to prevent, arriving by the
    other door.
    """
    try:
        address = tailscale_address()
    except TailnetUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    try:
        leases = tuple(load_leases(LEDGER_PATH))
        lease = own_lease(leases)
    except (LedgerError, NotDeclared) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    holder = SnapshotHolder(starting_snapshot(lease.host, leases, datetime.now()))

    if start_prober is None:
        start_prober = _default_prober
    start_prober(holder)

    try:
        server = server_factory((address, lease.port), make_handler(holder.get))
    except OSError as exc:
        # Someone else holds the leased port, or the tailnet address is not on
        # this machine yet. Both are the same refusal, and a line in the
        # journal beats a traceback.
        print(f"error: could not bind {address}:{lease.port}: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    print(f"harbor-console-web listening on http://{address}:{lease.port}/")
    try:
        server.serve_forever()  # type: ignore[attr-defined]
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()  # type: ignore[attr-defined]
    return EXIT_OK


def _default_prober(holder: SnapshotHolder) -> None:
    """Start the real prober thread against the real collectors.

    The ledger is re-read every cycle, so `ports sync` granting a lease shows
    up on the page without a restart. A daemon thread, because the process
    ends when the server does and there is nothing here to flush.
    """

    def collect() -> Snapshot:
        return collect_snapshot(load_leases(LEDGER_PATH), datetime.now())

    thread = threading.Thread(
        target=probe_loop, args=(holder, collect), name="harbor-prober", daemon=True
    )
    thread.start()


if __name__ == "__main__":
    raise SystemExit(main())
