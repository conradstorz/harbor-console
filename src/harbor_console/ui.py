"""UI rendering for Harbor Console."""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table


def build_dashboard(metrics: dict[str, str | float | int]) -> Panel:
    """Build a renderable dashboard panel from collected metrics."""
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("Metric", no_wrap=True)
    table.add_column("Value")

    table.add_row("Hostname", str(metrics["hostname"]))
    table.add_row("Uptime", str(metrics["uptime"]))
    table.add_row("CPU utilization", f"{float(metrics['cpu_utilization']):.1f}%")
    table.add_row("Memory utilization", f"{float(metrics['memory_utilization']):.1f}%")
    table.add_row("Disk utilization", f"{float(metrics['disk_utilization']):.1f}%")
    table.add_row("IPv4 address", str(metrics["ipv4_address"]))
    table.add_row("Docker container count", str(metrics["docker_container_count"]))
    table.add_row("Current date/time", str(metrics["current_datetime"]))

    return Panel(table, title="Harbor Console", border_style="white")
