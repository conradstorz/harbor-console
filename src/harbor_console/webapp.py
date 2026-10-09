"""The harbor-console-web entry point: one prober thread and one HTTP server.

One refusal, at startup: without a Tailscale address this process exits
non-zero rather than binding something broader. Binding *is* the access
control -- the page is an inventory of every service on the host -- so there
is no fallback address, no `--host`, and no dev mode (ADR 7). Traefik fronts
this page at its route; the direct bind stays tailnet-only.

After that, nothing takes the page down. Docker or Traefik being unreadable
is a banner. A cycle that fails leaves the last good snapshot standing with
the reason attached.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

from harbor_console.certificate import Certificate, CertificateUnavailable, served_certificate
from harbor_console.checks import OWN_ROUTE_HOST, run_checks
from harbor_console.directory import (
    KIND_HTTP,
    build_rows,
    declared_kind,
    find_findings,
    route_of,
    route_url,
)
from harbor_console.docker import DOCKER_UNAVAILABLE, Container, running_containers
from harbor_console.gpu import GpuEntry, collect_gpus
from harbor_console.inventory import build_inventory
from harbor_console.listening import LISTENING_UNAVAILABLE, Listener, listening_sockets
from harbor_console.probe import Health, probe
from harbor_console.snapshot import Snapshot
from harbor_console.storage import StorageEntry, collect_storage
from harbor_console.system import collect_system_metrics
from harbor_console.tailnet import TailnetUnavailable, tailscale_address
from harbor_console.traefik import TRAEFIK_UNAVAILABLE, Router, traefik_routers
from harbor_console.verdict import VERDICT_PATH, Verdict, write_verdict
from harbor_console.web import make_handler

#: Fixed. Traefik's file-provider route for `harbor.<host>` points here, and
#: there is deliberately no way to configure it (ADR 3, ADR 15).
WEB_PORT = 8100

PROBE_INTERVAL_SECONDS = 30.0

#: The credential this process presents to Traefik's own gated API (ADR 16).
#: The username is fixed -- there is only ever one caller -- and the
#: password lives in a file `install.sh` writes alongside the htpasswd file
#: Traefik itself reads, group-readable by `harbor` rather than root-only,
#: since this process runs as `harbor`, not root.
TRAEFIK_DASHBOARD_USER = "harbor"
TRAEFIK_DASHBOARD_PASSWORD_PATH = Path("/etc/traefik/dashboard-password")

EXIT_OK = 0
EXIT_REFUSED = 1


class SnapshotHolder:
    """The last snapshot the prober published. One writer, many readers."""

    def __init__(self, initial: Snapshot) -> None:
        self._lock = threading.Lock()
        self._snapshot = initial

    def get(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def set(self, snapshot: Snapshot) -> None:
        with self._lock:
            self._snapshot = snapshot


def starting_snapshot(host: str, now: datetime, tailnet_address: str | None = None) -> Snapshot:
    """The page's first answer, standing only until the prober's first cycle."""
    return Snapshot(
        collected=now,
        metrics={
            "hostname": host,
            "uptime": "collecting",
            "cpu_utilization": 0.0,
            "memory_summary": "collecting",
            "swap_summary": "collecting",
            "ipv4_address": "collecting",
            "docker_container_count": 0,
            "current_datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        },
        tailnet_address=tailnet_address,
    )


def read_traefik_credentials(
    path: Path = TRAEFIK_DASHBOARD_PASSWORD_PATH,
) -> tuple[str, str] | None:
    """This process's credential for Traefik's gated API, or None.

    Degrades like every other collector: a missing or empty file means
    either an older `install.sh` has not re-run since ADR 16, or the edge
    is not deployed at all. Read with no credentials, `traefik_routers`
    gets a 401 from the gate the same as any other reason Traefik could not
    be read, and the page shows the same banner it always has for that.
    """
    try:
        password = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not password:
        return None
    return (TRAEFIK_DASHBOARD_USER, password)


def collect_snapshot(
    now: datetime,
    collector: Callable[[], dict[str, str | float | int]] = collect_system_metrics,
    listeners: Callable[[], tuple[Listener, ...]] = listening_sockets,
    containers: Callable[[], tuple[Container, ...]] = running_containers,
    routers: Callable[[], tuple[Router, ...]] = traefik_routers,
    prober: Callable[[str], Health] = probe,
    storage: Callable[[], tuple[StorageEntry, ...]] = collect_storage,
    gpus: Callable[[], tuple[GpuEntry, ...]] = collect_gpus,
    certificate: Callable[[str], Certificate | CertificateUnavailable] = served_certificate,
    tailnet_address: str | None = None,
    own_port: int | None = WEB_PORT,
) -> Snapshot:
    """Gather every source once and fold it into one snapshot.

    Only HTTP rows with a known host are probed, at their route, so a green
    row proves the path through Traefik. The two sentinels are passed to the
    policy intact and only flattened into the snapshot for the renderer.
    """
    metrics = dict(collector())
    found = listeners()
    running = containers()
    routed = routers()

    health: dict[str, Health] = {}
    for container in running:
        if declared_kind(container) != KIND_HTTP:
            continue
        route = route_of(container)
        if route is None or route[1] is None:
            continue
        health[route[0]] = prober(route_url(route[1]))

    # The page's own route and the served certificate both need the edge's
    # address; without one they are not attempted, and the checks say so.
    own_route = prober(route_url(OWN_ROUTE_HOST)) if tailnet_address is not None else None
    served = certificate(tailnet_address) if tailnet_address is not None else None

    docker_available = running is not DOCKER_UNAVAILABLE
    traefik_available = routed is not TRAEFIK_UNAVAILABLE
    listeners_available = found is not LISTENING_UNAVAILABLE

    return Snapshot(
        collected=now,
        metrics=metrics,
        rows=build_rows(running, routed, found, health, probed=True),
        findings=find_findings(running, routed, found, tailnet_address, own_port=own_port),
        inventory=build_inventory(found, running, tailnet_address, own_port),
        containers=tuple(running),
        docker_available=docker_available,
        traefik_available=traefik_available,
        listeners_available=listeners_available,
        health=health,
        collection_error=None,
        probed=True,
        tailnet_address=tailnet_address,
        storage=storage(),
        gpus=gpus(),
        own_route=own_route,
        certificate=served,
        checks=run_checks(
            now=now,
            docker_available=docker_available,
            traefik_available=traefik_available,
            containers=running,
            routers=routed,
            own_route=own_route,
            certificate=served,
            listeners=found,
            listeners_available=listeners_available,
            tailnet_address=tailnet_address,
        ),
    )


def probe_loop(
    holder: SnapshotHolder,
    collect: Callable[[], Snapshot],
    sleep: Callable[[float], None] = time.sleep,
    interval: float = PROBE_INTERVAL_SECONDS,
    publish: Callable[[Snapshot], None] | None = None,
) -> None:
    """Publish a fresh snapshot on an interval until interrupted.

    A collection failure never takes the page down: the last good snapshot
    stands, with the reason attached. A successful cycle publishes
    `collection_error=None`, so a reason never outlives its cause.

    `publish` is how the verdict leaves this process (the file the console
    reads). It runs only after a good cycle, so a failed cycle lets the
    file go stale rather than restating a verdict from evidence the cycle
    did not have; and it is guarded, so a write that fails never stops the
    loop either.
    """
    while True:
        try:
            snapshot = collect()
        except Exception as exc:  # noqa: BLE001 - a supervisor loop, see above
            holder.set(replace(holder.get(), collection_error=str(exc) or exc.__class__.__name__))
        else:
            holder.set(snapshot)
            if publish is not None:
                try:
                    publish(snapshot)
                except Exception:  # noqa: BLE001 - the publisher reports its own failures
                    pass
        try:
            sleep(interval)
        except KeyboardInterrupt:
            return


def _report(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class VerdictPublisher:
    """Write one snapshot's checks to the verdict file.

    A write that fails is reported once per distinct error, not once per
    cycle: the same message every 30 s to the journal is noise, and a
    different one is news. The loop never sees the exception.
    """

    def __init__(
        self,
        path: Path = VERDICT_PATH,
        writer: Callable[[Verdict, Path], None] = write_verdict,
        report: Callable[[str], None] = _report,
    ) -> None:
        self._path = path
        self._writer = writer
        self._report = report
        self._last_error: str | None = None

    def __call__(self, snapshot: Snapshot) -> None:
        verdict = Verdict(
            written=snapshot.collected,
            hostname=str(snapshot.metrics.get("hostname", "")),
            checks=snapshot.checks,
        )
        try:
            self._writer(verdict, self._path)
        except Exception as exc:  # noqa: BLE001 - every failure to write must reach the journal
            message = f"could not write {self._path}: {exc}"
            if message != self._last_error:
                self._report(message)
                self._last_error = message
            return
        self._last_error = None


def main(
    argv: list[str] | None = None,
    server_factory: Callable[..., object] = ThreadingHTTPServer,
    start_prober: Callable[[SnapshotHolder, str], None] | None = None,
) -> int:
    """Entry point. Refuses to start rather than binding anything broader."""
    try:
        address = tailscale_address()
    except TailnetUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    host = str(collect_system_metrics()["hostname"])
    holder = SnapshotHolder(starting_snapshot(host, datetime.now(), tailnet_address=address))

    try:
        server = server_factory((address, WEB_PORT), make_handler(holder.get))
    except OSError as exc:
        print(f"error: could not bind {address}:{WEB_PORT}: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    if start_prober is None:
        start_prober = _default_prober
    start_prober(holder, address)

    print(f"harbor-console-web listening on http://{address}:{WEB_PORT}/")
    try:
        server.serve_forever()  # type: ignore[attr-defined]
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()  # type: ignore[attr-defined]
    return EXIT_OK


def _default_prober(holder: SnapshotHolder, tailnet_address: str) -> None:
    def collect() -> Snapshot:
        return collect_snapshot(
            datetime.now(),
            tailnet_address=tailnet_address,
            routers=lambda: traefik_routers(credentials=read_traefik_credentials()),
        )

    thread = threading.Thread(
        target=probe_loop,
        args=(holder, collect),
        kwargs={"publish": VerdictPublisher()},
        name="harbor-prober",
        daemon=True,
    )
    thread.start()


if __name__ == "__main__":
    raise SystemExit(main())
