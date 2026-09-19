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

from harbor_console.directory import (
    KIND_HTTP,
    build_rows,
    declared_kind,
    find_findings,
    route_of,
    route_url,
)
from harbor_console.docker import DOCKER_UNAVAILABLE, Container, running_containers
from harbor_console.listening import Listener, listening_sockets
from harbor_console.probe import Health, probe
from harbor_console.snapshot import Snapshot
from harbor_console.system import collect_system_metrics
from harbor_console.tailnet import TailnetUnavailable, tailscale_address
from harbor_console.traefik import TRAEFIK_UNAVAILABLE, Router, traefik_routers
from harbor_console.web import make_handler

#: Fixed. Traefik's file-provider route for `harbor.<host>` points here, and
#: there is deliberately no way to configure it (ADR 3, ADR 15).
WEB_PORT = 8100

PROBE_INTERVAL_SECONDS = 30.0

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
            "memory_utilization": 0.0,
            "disk_utilization": 0.0,
            "ipv4_address": "collecting",
            "docker_container_count": 0,
            "current_datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        },
        tailnet_address=tailnet_address,
    )


def collect_snapshot(
    now: datetime,
    collector: Callable[[], dict[str, str | float | int]] = collect_system_metrics,
    listeners: Callable[[], tuple[Listener, ...]] = listening_sockets,
    containers: Callable[[], tuple[Container, ...]] = running_containers,
    routers: Callable[[], tuple[Router, ...]] = traefik_routers,
    prober: Callable[[str], Health] = probe,
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

    return Snapshot(
        collected=now,
        metrics=metrics,
        rows=build_rows(running, routed, found, health, probed=True),
        findings=find_findings(running, routed, found, tailnet_address, own_port=own_port),
        listeners=found,
        containers=tuple(running),
        docker_available=running is not DOCKER_UNAVAILABLE,
        traefik_available=routed is not TRAEFIK_UNAVAILABLE,
        health=health,
        collection_error=None,
        probed=True,
        tailnet_address=tailnet_address,
    )


def probe_loop(
    holder: SnapshotHolder,
    collect: Callable[[], Snapshot],
    sleep: Callable[[float], None] = time.sleep,
    interval: float = PROBE_INTERVAL_SECONDS,
) -> None:
    """Publish a fresh snapshot on an interval until interrupted.

    A collection failure never takes the page down: the last good snapshot
    stands, with the reason attached. A successful cycle publishes
    `collection_error=None`, so a reason never outlives its cause.
    """
    while True:
        try:
            holder.set(collect())
        except Exception as exc:  # noqa: BLE001 - a supervisor loop, see above
            holder.set(replace(holder.get(), collection_error=str(exc) or exc.__class__.__name__))
        try:
            sleep(interval)
        except KeyboardInterrupt:
            return


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
        return collect_snapshot(datetime.now(), tailnet_address=tailnet_address)

    thread = threading.Thread(
        target=probe_loop, args=(holder, collect), name="harbor-prober", daemon=True
    )
    thread.start()


if __name__ == "__main__":
    raise SystemExit(main())
