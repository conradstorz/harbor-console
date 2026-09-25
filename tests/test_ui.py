from rich.console import Console

import harbor_console.system as system
from harbor_console.gpu import GpuEntry
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


def render(metrics, storage=(), gpus=()):
    """The panel as text. Wide enough that no cell wraps."""
    console = Console(width=120, record=True)
    console.print(build_dashboard(metrics, storage, gpus))
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

    assert any("GPU card0 (radeon)" in line and "35 °C" in line for line in lines)
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
