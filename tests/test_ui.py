from rich.console import Console

import harbor_console.system as system
from harbor_console.gpu import GpuEntry
from harbor_console.gpu_history import GpuAverages, WindowAverage
from harbor_console.storage import StorageEntry
from harbor_console.ui import build_dashboard

METRICS = {
    "hostname": "host-a",
    "uptime": "0d 00:01:40",
    "cpu_utilization": 33.5,
    "memory_summary": "4.0 / 32.0 GiB (12.5%)",
    "swap_summary": "0.0 / 8.0 GiB (0.0%)",
    "ipv4_address": "10.0.0.7",
    # 17 rather than a single digit: a lone "3" would also match "33.5%".
    "docker_container_count": 17,
    "current_datetime": "2026-08-01 00:00:00",
}


def render(metrics, storage=(), gpus=(), gpu_averages=()):
    """The panel as text. Wide enough that no cell wraps."""
    console = Console(width=120, record=True)
    console.print(build_dashboard(metrics, storage, gpus, None, gpu_averages))
    return console.export_text()


def test_dashboard_shows_every_metric():
    page = render(METRICS)

    assert "host-a" in page
    assert "0d 00:01:40" in page
    assert "33.5%" in page
    assert "10.0.0.7" in page
    assert "17" in page
    assert "2026-08-01 00:00:00" in page


def test_dashboard_shows_memory_with_its_scale():
    page = render(METRICS)

    assert "4.0 / 32.0 GiB (12.5%)" in page


def test_dashboard_shows_swap_on_its_own_row():
    page = render(METRICS)

    assert "Swap" in page
    assert "0.0 / 8.0 GiB (0.0%)" in page


def test_dashboard_pairs_each_label_with_its_own_value():
    """Pins which value sits beside which label.

    The two "appears somewhere in the page" tests above would both still pass
    if the Memory and Swap rows had their values swapped -- a host at 85%
    memory would render `0.0 / 8.0 GiB (0.0%)` beside "Memory" and look idle.
    """
    lines = render(METRICS).splitlines()

    assert any("Memory" in line and "4.0 / 32.0 GiB (12.5%)" in line for line in lines)
    assert any("Swap" in line and "0.0 / 8.0 GiB (0.0%)" in line for line in lines)


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


def test_dashboard_shows_one_row_per_storage_entry():
    storage = (
        StorageEntry(label="/", used=65 * 1024**3, total=98 * 1024**3, percent=70.0),
        StorageEntry(label="VG ubuntu-vg", total=251111931904, note="unallocated"),
    )

    page = render(METRICS, storage)

    assert "65.0 / 98.0 GiB (70.0%)" in page
    assert "233.9 GiB unallocated" in page
    assert "Disk utilization" not in page
    # "VG ubuntu-vg" is unambiguous, so a plain substring check proves its
    # label survived the render.
    assert "VG ubuntu-vg" in page
    # A bare "/" is too ambiguous for a substring check -- it's also the
    # separator inside "65.0 / 98.0 GiB (70.0%)" and part of the panel's own
    # border characters. Pin it to its own cell instead: the exported text
    # renders each row as "│ <label>   <value>", so the token right after
    # the panel's left border is the label column.
    assert any(line.split()[1:2] == ["/"] for line in page.splitlines())


def test_dashboard_shows_one_row_per_gpu():
    gpus = (
        GpuEntry(label="GPU card0 (radeon)", driver="radeon", temp_c=35.0),
        GpuEntry(label="GPU card1 (amdgpu)", driver="amdgpu", busy_percent=7),
    )

    lines = render(METRICS, (), gpus).splitlines()

    assert any("GPU card0 (radeon)" in line and "35°C" in line for line in lines)
    assert any("GPU card1 (amdgpu)" in line and "busy 7%" in line for line in lines)


def test_dashboard_says_none_detected_when_there_are_no_gpus():
    storage = (StorageEntry(label="VG ubuntu-vg", total=251111931904, note="unallocated"),)

    page = render(METRICS, storage)
    lines = page.splitlines()

    assert any(line.split()[1:2] == ["GPU"] and "none detected" in line for line in lines)
    assert page.index("VG ubuntu-vg") < page.index("none detected") < page.index("IPv4 address")


def test_dashboard_puts_gpu_rows_between_storage_and_ipv4():
    storage = (StorageEntry(label="VG ubuntu-vg", total=251111931904, note="unallocated"),)
    gpus = (GpuEntry(label="GPU card0 (radeon)", driver="radeon", temp_c=35.0),)

    page = render(METRICS, storage, gpus)

    assert page.index("VG ubuntu-vg") < page.index("GPU card0") < page.index("IPv4 address")


def test_dashboard_keeps_the_fullest_gpu_row_on_one_line_at_80_columns():
    """The console is 80 cells wide (ADR 20); a wrapped GPU row would eat one
    of its three rows of headroom."""
    gpus = (
        GpuEntry(
            label="GPU card1 (RTX 3060)",
            driver="nvidia",
            busy_percent=100,
            vram_used=int(11.9 * 1024**3),
            vram_total=12 * 1024**3,
            temp_c=100.0,
            power_w=170.0,
            power_limit_w=170.0,
            fan_percent=100,
        ),
    )

    console = Console(width=80, record=True)
    console.print(build_dashboard(METRICS, (), gpus))
    lines = console.export_text().splitlines()

    assert any("GPU card1 (RTX 3060)" in line and "fan 100%" in line for line in lines)
    assert any("Docker containers" in line and "17" in line for line in lines)


AVERAGES = (
    GpuAverages(
        "card1",
        (
            WindowAverage("1h", 3600, 42, 3600),
            WindowAverage("3h", 3 * 3600, 38, 3 * 3600),
            WindowAverage("7h", 7 * 3600, 30, 7 * 3600),
            WindowAverage("24h", 86400, 25, 86400),
            WindowAverage("7d", 7 * 86400, 18, 2 * 86400),
        ),
    ),
)


def test_dashboard_shows_the_busy_averages_under_the_matching_card():
    gpus = (GpuEntry(label="GPU card1 (RTX 3060)", card="card1", driver="nvidia", busy_percent=7),)

    lines = render(METRICS, (), gpus, AVERAGES).splitlines()
    gpu_row = next(i for i, line in enumerate(lines) if "GPU card1 (RTX 3060)" in line)

    assert "busy avg" in lines[gpu_row + 1]
    assert "1h 42% · 3h 38% · 7h 30% · 24h 25% · 7d 18% (2d)" in lines[gpu_row + 1]


def test_dashboard_matches_averages_by_bus_id():
    gpus = (
        GpuEntry(
            label="GPU card1 (RTX 3060)",
            card="card1",
            bus_id="0000:02:00.0",
            driver="nvidia",
            busy_percent=7,
        ),
    )
    averages = (
        GpuAverages(
            "0000:02:00.0",
            (WindowAverage("1h", 3600, 42, 3600),),
        ),
    )

    lines = render(METRICS, (), gpus, averages).splitlines()
    gpu_row = next(i for i, line in enumerate(lines) if "GPU card1 (RTX 3060)" in line)

    assert "busy avg" in lines[gpu_row + 1]
    assert "1h 42%" in lines[gpu_row + 1]


def test_dashboard_says_not_reported_for_a_card_without_averages():
    gpus = (GpuEntry(label="GPU card0 (radeon)", card="card0", driver="radeon", temp_c=35.0),)

    lines = render(METRICS, (), gpus, AVERAGES).splitlines()
    gpu_row = next(i for i, line in enumerate(lines) if "GPU card0 (radeon)" in line)

    assert "busy avg" in lines[gpu_row + 1]
    assert "not reported" in lines[gpu_row + 1]


def test_dashboard_says_not_reported_when_there_is_no_verdict_at_all():
    gpus = (GpuEntry(label="GPU card1 (RTX 3060)", card="card1", driver="nvidia", busy_percent=7),)

    page = render(METRICS, (), gpus)

    assert "busy avg" in page
    assert "not reported" in page


def test_dashboard_gives_the_unavailable_sentinel_no_averages_row():
    gpus = (GpuEntry(label="GPU", note="unavailable"),)

    page = render(METRICS, (), gpus, AVERAGES)

    assert "unavailable" in page
    assert "busy avg" not in page


def test_dashboard_gives_no_averages_row_when_there_are_no_gpus():
    page = render(METRICS, (), (), AVERAGES)

    assert "none detected" in page
    assert "busy avg" not in page


def test_dashboard_keeps_the_steady_averages_row_on_one_line_at_80_columns():
    gpus = (GpuEntry(label="GPU card1 (RTX 3060)", card="card1", driver="nvidia", busy_percent=100),)
    full = (
        GpuAverages(
            "card1",
            (
                WindowAverage("1h", 3600, 100, 3600),
                WindowAverage("3h", 3 * 3600, 100, 3 * 3600),
                WindowAverage("7h", 7 * 3600, 100, 7 * 3600),
                WindowAverage("24h", 86400, 100, 86400),
                WindowAverage("7d", 7 * 86400, 100, 6 * 86400),
            ),
        ),
    )

    console = Console(width=80, record=True)
    console.print(build_dashboard(METRICS, (), gpus, None, full))
    lines = console.export_text().splitlines()

    assert any("busy avg" in line and "7d 100% (6d)" in line for line in lines)


def test_dashboard_accepts_the_old_three_argument_call():
    gpus = (GpuEntry(label="GPU card0 (radeon)", card="card0", driver="radeon", temp_c=35.0),)
    console = Console(width=120, record=True)

    console.print(build_dashboard(METRICS, (), gpus))
    page = console.export_text()

    assert "GPU card0 (radeon)" in page
    assert "busy avg" in page


from datetime import datetime, timedelta

from rich.console import Console as _Console

from harbor_console.checks import STATE_FAILED, STATE_OK, Check
from harbor_console.ui import build_banner
from harbor_console.verdict import Verdict

NOW = datetime(2026, 10, 9, 17, 21, 46)
STARTED = NOW - timedelta(minutes=10)


def verdict(checks, written=NOW):
    return Verdict(written=written, hostname="host-a", checks=tuple(checks))


def banner_lines(banner):
    console = _Console(width=80, record=True)
    console.print(banner)
    return console.export_text().splitlines()


def test_no_banner_when_nothing_failed():
    assert build_banner(verdict([Check("docker", STATE_OK, "answered")]), NOW, STARTED) is None


def test_banner_carries_the_one_colour_on_the_surface():
    from harbor_console import ui

    banner = build_banner(verdict([Check("docker", STATE_FAILED, "could not be read")]), NOW, STARTED)

    assert banner is not None
    assert banner.style == ui.BANNER_STYLE
    assert "red" in str(ui.BANNER_STYLE)


def test_banner_names_the_failed_checks_on_three_rows():
    checks = [
        Check("docker", STATE_OK, "answered"),
        Check("docker-provider", STATE_FAILED, "8 containers declare a route but Traefik reports no @docker router"),
        Check("own-route", STATE_FAILED, "https://harbor.hpz440.ohr3023.org/ did not answer through the proxy"),
    ]

    lines = banner_lines(build_banner(verdict(checks), NOW, STARTED))

    assert len(lines) == 3
    assert lines[0].startswith("PLATFORM BROKEN")
    assert lines[1].startswith("docker-provider: 8 containers")
    assert lines[2].startswith("own-route: https://harbor")
    assert all(len(line) <= 80 for line in lines)


def test_banner_pads_a_single_failure_to_three_rows():
    lines = banner_lines(build_banner(verdict([Check("docker", STATE_FAILED, "could not be read")]), NOW, STARTED))

    assert len(lines) == 3
    assert lines[1].startswith("docker: could not be read")


def test_banner_collapses_a_third_failure_and_beyond():
    checks = [Check(f"c{i}", STATE_FAILED, "x") for i in range(5)]

    lines = banner_lines(build_banner(verdict(checks), NOW, STARTED))

    assert lines[1].startswith("c0: x")
    assert "+4 more, see the status page" in lines[2]


def test_banner_when_the_verdict_is_stale():
    stale = verdict([Check("docker", STATE_OK, "answered")], written=NOW - timedelta(minutes=5))

    lines = banner_lines(build_banner(stale, NOW, STARTED))

    assert len(lines) == 3
    assert lines[1].startswith("status page has not reported since 17:16:46")


def test_banner_when_the_file_is_missing_long_after_start():
    lines = banner_lines(build_banner(None, NOW, STARTED))

    assert lines[1].startswith("status page has not reported since 17:11:46")


def test_no_banner_while_the_file_is_missing_just_after_start():
    assert build_banner(None, NOW, NOW - timedelta(seconds=30)) is None


def test_dashboard_puts_the_banner_above_the_panel_within_80_columns():
    banner = build_banner(verdict([Check("docker", STATE_FAILED, "could not be read")]), NOW, STARTED)

    console = _Console(width=80, record=True)
    console.print(build_dashboard(METRICS, (), (), banner))
    lines = console.export_text().splitlines()

    assert lines[0].startswith("PLATFORM BROKEN")
    assert "Harbor Console" in lines[3]
    assert any("Docker containers" in line and "17" in line for line in lines)


def test_dashboard_without_a_banner_is_unchanged():
    console = _Console(width=80, record=True)
    console.print(build_dashboard(METRICS, (), ()))
    lines = console.export_text().splitlines()

    assert "Harbor Console" in lines[0]
