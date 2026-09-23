from rich.console import Console

import harbor_console.system as system
from harbor_console.ui import build_dashboard

METRICS = {
    "hostname": "host-a",
    "uptime": "0d 00:01:40",
    "cpu_utilization": 33.5,
    "memory_summary": "4.0 / 32.0 GiB (12.5%)",
    "swap_summary": "0.0 / 8.0 GiB (0.0%)",
    "disk_utilization": 78.0,
    "ipv4_address": "10.0.0.7",
    "docker_container_count": 3,
    "current_datetime": "2026-08-01 00:00:00",
}


def render(metrics):
    """The panel as text. Wide enough that no cell wraps."""
    console = Console(width=120, record=True)
    console.print(build_dashboard(metrics))
    return console.export_text()


def test_dashboard_shows_every_metric():
    page = render(METRICS)

    assert "host-a" in page
    assert "0d 00:01:40" in page
    assert "33.5%" in page
    assert "78.0%" in page
    assert "10.0.0.7" in page
    assert "2026-08-01 00:00:00" in page


def test_dashboard_shows_memory_with_its_scale():
    page = render(METRICS)

    assert "4.0 / 32.0 GiB (12.5%)" in page


def test_dashboard_shows_swap_on_its_own_row():
    page = render(METRICS)

    assert "Swap" in page
    assert "0.0 / 8.0 GiB (0.0%)" in page


def test_dashboard_reports_a_host_with_no_swap():
    page = render(dict(METRICS, swap_summary="none configured"))

    assert "none configured" in page


def test_the_renderer_and_the_collector_agree_on_every_key(monkeypatch):
    """The contract CLAUDE.md describes, enforced.

    The two collectors that reach outside this process are stubbed -- one
    shells out to `docker`, the other opens a socket -- because tests here use
    neither. Everything else is the real collector, so a key renamed on one
    side and not the other fails here with a KeyError.
    """
    monkeypatch.setattr(system, "get_docker_container_count", lambda: 0)
    monkeypatch.setattr(system, "get_ipv4_address", lambda: "127.0.0.1")

    render(system.collect_system_metrics())
