"""Application entrypoint and refresh loop."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable

from rich.live import Live

from harbor_console.system import collect_system_metrics
from harbor_console.ui import build_dashboard


MetricsCollector = Callable[[], dict[str, str | float | int]]
DashboardBuilder = Callable[[dict[str, str | float | int]], object]


def run(
    refresh_interval: float = 1.0,
    collector: MetricsCollector = collect_system_metrics,
    renderer: DashboardBuilder = build_dashboard,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run the Harbor Console refresh loop."""
    try:
        initial_metrics = collector()
        with Live(renderer(initial_metrics), refresh_per_second=4, screen=True) as live:
            while True:
                sleep(refresh_interval)
                live.update(renderer(collector()))
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. A bare invocation runs the tty1 dashboard."""
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "ports":
        from harbor_console.ports import cli

        return cli.main(args[1:])
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
