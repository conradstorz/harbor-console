"""Application entrypoint and refresh loop."""

from __future__ import annotations

import time
from collections.abc import Callable

from rich.live import Live

from harbor_console.gpu import GpuEntry, collect_gpus
from harbor_console.storage import StorageEntry, collect_storage
from harbor_console.system import collect_system_metrics
from harbor_console.ui import build_dashboard


MetricsCollector = Callable[[], dict[str, str | float | int]]
StorageCollector = Callable[[], tuple[StorageEntry, ...]]
GpuCollector = Callable[[], tuple[GpuEntry, ...]]
DashboardBuilder = Callable[
    [dict[str, str | float | int], tuple[StorageEntry, ...], tuple[GpuEntry, ...]], object
]


def run(
    refresh_interval: float = 1.0,
    collector: MetricsCollector = collect_system_metrics,
    renderer: DashboardBuilder = build_dashboard,
    sleep: Callable[[float], None] = time.sleep,
    storage_collector: StorageCollector = collect_storage,
    gpu_collector: GpuCollector = collect_gpus,
) -> int:
    """Run the Harbor Console refresh loop."""

    def frame() -> object:
        return renderer(collector(), storage_collector(), gpu_collector())

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
