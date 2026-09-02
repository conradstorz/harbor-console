"""The harbor-console-web entry point: one prober thread and one HTTP server.

The two processes of this project have independent lifetimes. Logging in at the
attached monitor must not take this page down, and this page must not depend on
anyone being logged in.

Four refusals, all at startup, all deliberate. Without a Tailscale address,
without a readable ledger, without a lease of its own, or with more than one
lease that could be its own, this process exits non-zero rather than binding
something broader or serving something it cannot vouch for; systemd restarts
it, and an operator reads the reason in journald.
Binding *is* the access control -- the page is an inventory of every service on
the host -- so there is no fallback address, no `--host`, and no dev mode
(ADR 7).

After that, nothing takes the page down. The prober thread collects on an
interval and publishes one snapshot; a cycle that fails leaves the last good
snapshot standing with the reason attached, because a page whose whole job is
telling you something is wrong must not become the thing that is wrong.

The host this process serves is decided once, here, from the lease it holds,
and passed down explicitly. It is never re-derived from `socket.gethostname()`:
the ledger's `host` is a hand-authored string, and a name that disagrees with
the OS -- `hpz440` against `hpz440.lan` -- would silently empty this host's
share of the ledger, reporting every correctly-running container as undeclared
and every dead service as nothing at all.
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


class AmbiguousDeclaration(Exception):
    """The ledger carries more than one lease that could be this service's."""


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

    So is an ambiguous one. The ledger key is `(host, addr, port)` and the
    ledger is fleet-wide by design, so two machines may each legitimately
    declare `harbor-console`/`web`; taking the first one found would bind a
    port this host may not hold, and identify the page as a machine it is not.
    There is no hostname tiebreak on purpose -- that is the very comparison
    this module refuses to make (see the module docstring) -- and multi-host
    operation is deferred, so refusing and naming the candidates is the honest
    answer. One lease is the reality today; this keeps the other case loud
    rather than silently arbitrary.
    """
    mine = [
        lease
        for lease in leases
        if lease.project == WEB_PROJECT and lease.name == WEB_PORT_NAME
    ]
    if not mine:
        raise NotDeclared(
            f"no lease for {WEB_PROJECT}/{WEB_PORT_NAME}; this service must be declared"
        )
    if len(mine) > 1:
        # Naming the host alone is not enough to tell leases apart: a
        # hand-edited ledger can declare the same service twice on the same
        # host, on different addresses or ports, and "hpz440, hpz440" gives
        # an operator nothing to fix. addr:port distinguishes them.
        found = ", ".join(
            f"{lease.host} ({lease.addr}:{lease.port})"
            for lease in sorted(mine, key=lambda lease: (lease.host, lease.addr, lease.port))
        )
        raise AmbiguousDeclaration(
            f"{len(mine)} leases for {WEB_PROJECT}/{WEB_PORT_NAME}, on {found}; "
            "running on more than one host needs an explicit choice of "
            "identity, which this service does not have"
        )
    return mine[0]


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
    host: str,
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

    `host` is decided once at startup from the lease this process holds and
    passed in; it is never re-derived from the OS. The ledger's `host` is a
    hand-authored string that nothing validates against `gethostname()`, and
    `find_drift` keeps only `lease.host == host`, so one name mismatch --
    `hpz440` against `hpz440.lan` -- empties this host's share of the ledger:
    every correctly-running container is reported undeclared and the whole
    `declared-not-running` class, the dead service this page exists to show,
    goes silent with nothing saying why.

    That decided host also replaces the collector's `hostname`, so the page's
    heading and `/ports.json` name the host the ledger names. The allocator
    compares the payload's `host` against the declaration's
    (`ports/allocate.py`) and discards live evidence when they differ, so
    publishing the OS name there would quietly cost it the socket evidence it
    fetched `/ports.json` to get. The dict is copied rather than edited: the
    collector's return value is not ours to mutate.
    """
    held = tuple(leases)
    metrics = dict(collector())
    metrics["hostname"] = host
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
        collection_error=None,
        probed=True,
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
    cycle publishes `collection_error=None`, so a reason never outlives its
    cause.

    What the reason is called matters: every one of those failures used to be
    published as a ledger error, so a psutil that raised sent the operator to
    read `services.toml`. The field says what it holds -- a failed cycle --
    and `_reason` names a `LedgerError` as one so the common case still reads
    plainly.
    """
    while True:
        try:
            holder.set(collect())
        except Exception as exc:  # noqa: BLE001 - a supervisor loop, see above
            holder.set(replace(holder.get(), collection_error=_reason(exc)))
        try:
            sleep(interval)
        except KeyboardInterrupt:
            return


def _reason(exc: Exception) -> str:
    """Describe a failed cycle, even when the exception carries no message.

    A `LedgerError` is named as one, because an unreadable `services.toml` is
    the common case and the operator's next move depends on knowing it. Every
    other failure is reported as itself rather than dressed as a ledger fault.
    """
    text = str(exc) or exc.__class__.__name__
    if isinstance(exc, LedgerError):
        return f"the lease ledger could not be read: {text}"
    return text


def main(
    argv: list[str] | None = None,
    server_factory: Callable[..., object] = ThreadingHTTPServer,
    start_prober: Callable[[SnapshotHolder, str], None] | None = None,
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
    except (LedgerError, NotDeclared, AmbiguousDeclaration) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    # The one place the served host is decided, and the only place it can be:
    # the lease this process holds. Everything downstream is handed this value.
    host = lease.host
    holder = SnapshotHolder(starting_snapshot(host, leases, datetime.now()))

    try:
        server = server_factory((address, lease.port), make_handler(holder.get))
    except OSError as exc:
        # Someone else holds the leased port, or the tailnet address is not on
        # this machine yet. Both are the same refusal, and a line in the
        # journal beats a traceback.
        print(f"error: could not bind {address}:{lease.port}: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    # Only now, with the port actually held. Starting the prober first made a
    # refusal expensive: a bind that fails would still have fired a whole
    # collection cycle -- `docker ps` plus an HTTP probe of every leased port
    # -- on its way out, and `RestartSec=2` repeats that every two seconds for
    # as long as the port stays taken. The bind is also what `starting_snapshot`
    # promises comes first.
    if start_prober is None:
        start_prober = _default_prober
    start_prober(holder, host)

    print(f"harbor-console-web listening on http://{address}:{lease.port}/")
    try:
        server.serve_forever()  # type: ignore[attr-defined]
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()  # type: ignore[attr-defined]
    return EXIT_OK


def _default_prober(holder: SnapshotHolder, host: str) -> None:
    """Start the real prober thread against the real collectors.

    The ledger is re-read every cycle, so `ports sync` granting a lease shows
    up on the page without a restart. `host` is not: it is the identity this
    process started with, closed over here so no cycle can quietly reconcile
    against a different machine than the one it bound for. A daemon thread,
    because the process ends when the server does and there is nothing here
    to flush.
    """

    def collect() -> Snapshot:
        return collect_snapshot(load_leases(LEDGER_PATH), host, datetime.now())

    thread = threading.Thread(
        target=probe_loop, args=(holder, collect), name="harbor-prober", daemon=True
    )
    thread.start()


if __name__ == "__main__":
    raise SystemExit(main())
