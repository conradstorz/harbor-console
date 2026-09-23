import subprocess
from types import SimpleNamespace

import harbor_console.system as system


def test_format_uptime():
    assert system.format_uptime(90061) == "1d 01:01:01"


def test_get_docker_container_count_returns_zero_on_missing_docker():
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError

    assert system.get_docker_container_count(run=fake_run) == 0


def test_collect_system_metrics(monkeypatch):
    fake_now = SimpleNamespace(
        timestamp=lambda: 1_000.0,
        strftime=lambda _fmt: "2026-08-01 00:00:00",
    )
    memory = SimpleNamespace(
        percent=13.0, total=32 * 1024**3, available=28 * 1024**3
    )

    monkeypatch.setattr(system, "datetime", SimpleNamespace(now=lambda: fake_now))
    monkeypatch.setattr(system.psutil, "boot_time", lambda: 900.0)
    monkeypatch.setattr(system.psutil, "cpu_percent", lambda interval=None: 12.5)
    monkeypatch.setattr(system.psutil, "virtual_memory", lambda: memory)
    monkeypatch.setattr(system.psutil, "disk_usage", lambda _path: SimpleNamespace(percent=78.0))
    monkeypatch.setattr(system.socket, "gethostname", lambda: "host-a")
    monkeypatch.setattr(system, "get_ipv4_address", lambda: "10.0.0.7")
    monkeypatch.setattr(system, "get_docker_container_count", lambda: 3)
    monkeypatch.setattr(system, "get_swap_summary", lambda: "0.0 / 8.0 GiB (0.0%)")

    metrics = system.collect_system_metrics()

    assert metrics == {
        "hostname": "host-a",
        "uptime": "0d 00:01:40",
        "cpu_utilization": 12.5,
        "memory_utilization": 13.0,
        "memory_summary": "4.0 / 32.0 GiB (13.0%)",
        "swap_summary": "0.0 / 8.0 GiB (0.0%)",
        "disk_utilization": 78.0,
        "ipv4_address": "10.0.0.7",
        "docker_container_count": 3,
        "current_datetime": "2026-08-01 00:00:00",
    }


def test_docker_count_gives_the_subprocess_a_timeout():
    seen = {}

    def run(*_args, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(stdout="", returncode=0)

    system.get_docker_container_count(run=run)

    assert seen["timeout"] == 2.0


def test_docker_count_treats_a_hung_daemon_as_zero():
    def run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["docker", "ps", "-q"], timeout=2.0)

    assert system.get_docker_container_count(run=run) == 0


def test_format_bytes_counts_gibibytes():
    assert system.format_bytes(1024**3) == "1.0"
    assert system.format_bytes(0) == "0.0"
    assert system.format_bytes(1536 * 1024**2) == "1.5"


def test_format_usage_pairs_the_bytes_with_the_percent():
    assert system.format_usage(4 * 1024**3, 32 * 1024**3, 12.97) == "4.0 / 32.0 GiB (13.0%)"


def test_swap_summary_reports_used_of_total():
    swap = SimpleNamespace(total=8 * 1024**3, used=2 * 1024**3, percent=25.0)

    assert system.get_swap_summary(swap_memory=lambda: swap) == "2.0 / 8.0 GiB (25.0%)"


def test_swap_summary_says_none_configured_when_there_is_no_swap():
    swap = SimpleNamespace(total=0, used=0, percent=0.0)

    assert system.get_swap_summary(swap_memory=lambda: swap) == "none configured"


def test_swap_summary_degrades_when_psutil_cannot_answer():
    def boom():
        raise RuntimeError("no swap on this platform")

    assert system.get_swap_summary(swap_memory=boom) == "unavailable"
