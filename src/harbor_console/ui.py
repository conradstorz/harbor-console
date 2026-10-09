"""UI rendering for Harbor Console.

Renders only. The one piece of colour on this surface is the platform
banner (ADR 21): red, three rows, above the dashboard, and only while the
verdict says the host's own machinery is broken or the status page has
stopped reporting. Healthy renders exactly what it rendered before the
banner existed, so the three rows of headroom ADR 20 leaves stay free.
"""

from __future__ import annotations

from datetime import datetime

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from harbor_console.checks import STATE_FAILED, is_stale, platform_broken
from harbor_console.gpu import GpuEntry, format_gpu
from harbor_console.storage import StorageEntry, format_entry
from harbor_console.verdict import Verdict

BANNER_ROWS = 3
#: The console is 80 cells wide (ADR 20). Rows are padded to it so the red
#: background is a solid block, and truncated to it so nothing wraps into
#: the headroom.
BANNER_WIDTH = 80
BANNER_STYLE = "bold white on red"


def build_banner(verdict: Verdict | None, now: datetime, missing_since: datetime) -> Text | None:
    """Three rows of red, or None when there is nothing to shout about.

    A missing file is judged against `missing_since` (when this console
    started) the same way a present one is judged against its `written`:
    a web service that has not written for 90 s has gone quiet, and so has
    one that never wrote in the 90 s since the console came up. Within that
    window a missing file is just a page still starting.
    """
    if verdict is None:
        if not is_stale(missing_since, now):
            return None
        lines = [f"status page has not reported since {missing_since:%H:%M:%S}"]
    elif is_stale(verdict.written, now):
        lines = [f"status page has not reported since {verdict.written:%H:%M:%S}"]
    elif platform_broken(verdict.checks):
        failed = [c for c in verdict.checks if c.state == STATE_FAILED]
        lines = [f"{c.name}: {c.reason}" for c in failed[: BANNER_ROWS - 1]]
        if len(failed) > BANNER_ROWS - 1:
            lines[-1] = f"+{len(failed) - (BANNER_ROWS - 2)} more, see the status page"
    else:
        return None

    rows = ["PLATFORM BROKEN", *lines]
    rows += [""] * (BANNER_ROWS - len(rows))
    padded = [row[:BANNER_WIDTH].ljust(BANNER_WIDTH) for row in rows[:BANNER_ROWS]]
    return Text("\n".join(padded), style=BANNER_STYLE, no_wrap=True, overflow="ellipsis")


def build_dashboard(
    metrics: dict[str, str | float | int],
    storage: tuple[StorageEntry, ...] = (),
    gpus: tuple[GpuEntry, ...] = (),
    banner: Text | None = None,
) -> Panel | Group:
    """Build a renderable dashboard panel from collected metrics, storage and GPUs."""
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("Metric", no_wrap=True)
    table.add_column("Value")

    table.add_row("Hostname", str(metrics["hostname"]))
    table.add_row("Uptime", str(metrics["uptime"]))
    table.add_row("CPU utilization", f"{float(metrics['cpu_utilization']):.1f}%")
    table.add_row("Memory", str(metrics["memory_summary"]))
    table.add_row("Swap", str(metrics["swap_summary"]))
    for entry in storage:
        table.add_row(entry.label, format_entry(entry))
    # An empty tuple is a host with no card, and that is a fact worth a row:
    # a blank where the GPU line should be reads as a render bug.
    if gpus:
        for gpu in gpus:
            table.add_row(gpu.label, format_gpu(gpu))
    else:
        table.add_row("GPU", "none detected")
    table.add_row("IPv4 address", str(metrics["ipv4_address"]))
    table.add_row("Docker containers", str(metrics["docker_container_count"]))
    table.add_row("Current date/time", str(metrics["current_datetime"]))

    panel = Panel(table, title="Harbor Console", border_style="white")
    if banner is None:
        return panel
    return Group(banner, panel)
