"""Application entrypoint and refresh loop."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

from rich.live import Live

from harbor_console.gpu import GpuEntry, collect_gpus
from harbor_console.gpu_history import GpuAverages
from harbor_console.storage import StorageEntry, collect_storage
from harbor_console.system import collect_system_metrics
from harbor_console.ui import build_banner, build_dashboard
from harbor_console.verdict import Verdict, read_verdict


MetricsCollector = Callable[[], dict[str, str | float | int]]
StorageCollector = Callable[[], tuple[StorageEntry, ...]]
GpuCollector = Callable[[], tuple[GpuEntry, ...]]
VerdictReader = Callable[[], Verdict | None]
DashboardBuilder = Callable[
    [
        dict[str, str | float | int],
        tuple[StorageEntry, ...],
        tuple[GpuEntry, ...],
        object,
        tuple[GpuAverages, ...],
    ],
    object,
]


def run(
    refresh_interval: float = 1.0,
    collector: MetricsCollector = collect_system_metrics,
    renderer: DashboardBuilder = build_dashboard,
    sleep: Callable[[float], None] = time.sleep,
    storage_collector: StorageCollector = collect_storage,
    gpu_collector: GpuCollector = collect_gpus,
    verdict_reader: VerdictReader = read_verdict,
    clock: Callable[[], datetime] = datetime.now,
) -> int:
    """Run the Harbor Console refresh loop.

    The verdict is read from a file once per tick and never probed here:
    this loop runs at 1 Hz on tty1 and must never wait on a socket. A
    missing file is judged against when this loop started (`build_banner`).
    The same read carries the GPU busy averages the prober keeps; the
    console computes none of its own (ADR 22).
    """
    started = clock()

    def frame() -> object:
        now = clock()
        verdict = verdict_reader()
        banner = build_banner(verdict, now, started)
        averages = verdict.gpus if verdict is not None else ()
        return renderer(collector(), storage_collector(), gpu_collector(), banner, averages)

    try:
        with Live(frame(), refresh_per_second=4, screen=True) as live:
            while True:
                sleep(refresh_interval)
                live.update(frame())
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. A bare invocation runs the tty1 dashboard."""
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
