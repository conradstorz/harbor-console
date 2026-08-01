from types import SimpleNamespace

import harbor_console.system as system


def test_format_uptime():
    assert system.format_uptime(90061) == "1d 01:01:01"


def test_get_docker_container_count_returns_zero_on_missing_docker(monkeypatch):
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(system.subprocess, "run", fake_run)

    assert system.get_docker_container_count() == 0


def test_collect_system_metrics(monkeypatch):
    fake_now = SimpleNamespace(
        timestamp=lambda: 1_000.0,
        strftime=lambda _fmt: "2026-08-01 00:00:00",
    )

    monkeypatch.setattr(system, "datetime", SimpleNamespace(now=lambda: fake_now))
    monkeypatch.setattr(system.psutil, "boot_time", lambda: 900.0)
    monkeypatch.setattr(system.psutil, "cpu_percent", lambda interval=None: 12.5)
    monkeypatch.setattr(system.psutil, "virtual_memory", lambda: SimpleNamespace(percent=45.0))
    monkeypatch.setattr(system.psutil, "disk_usage", lambda _path: SimpleNamespace(percent=78.0))
    monkeypatch.setattr(system.socket, "gethostname", lambda: "host-a")
    monkeypatch.setattr(system, "get_ipv4_address", lambda: "10.0.0.7")
    monkeypatch.setattr(system, "get_docker_container_count", lambda: 3)

    metrics = system.collect_system_metrics()

    assert metrics == {
        "hostname": "host-a",
        "uptime": "0d 00:01:40",
        "cpu_utilization": 12.5,
        "memory_utilization": 45.0,
        "disk_utilization": 78.0,
        "ipv4_address": "10.0.0.7",
        "docker_container_count": 3,
        "current_datetime": "2026-08-01 00:00:00",
    }
