from harbor_console import app


class DummyLive:
    def __init__(self, initial, **_kwargs):
        self.initial = initial
        self.updated = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def update(self, renderable):
        self.updated.append(renderable)


def test_run_updates_dashboard_and_exits_cleanly(monkeypatch):
    calls = {"count": 0}

    def collector():
        calls["count"] += 1
        return {"tick": calls["count"]}

    def renderer(metrics, _storage, _gpus, _banner):
        return f"render-{metrics['tick']}"

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "Live", DummyLive)

    result = app.run(
        refresh_interval=1.0,
        collector=collector,
        renderer=renderer,
        sleep=fake_sleep,
        storage_collector=lambda: (),
        gpu_collector=lambda: (),
    )

    assert result == 0
    assert calls["count"] == 1


def test_run_passes_storage_to_the_renderer(monkeypatch):
    seen = {}

    def renderer(metrics, storage, _gpus, _banner):
        seen["storage"] = storage
        return "rendered"

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "Live", DummyLive)

    result = app.run(
        collector=lambda: {"tick": 1},
        renderer=renderer,
        sleep=fake_sleep,
        storage_collector=lambda: ("entry",),
        gpu_collector=lambda: (),
    )

    assert result == 0
    assert seen["storage"] == ("entry",)


def test_run_passes_gpus_to_the_renderer(monkeypatch):
    seen = {}

    def renderer(metrics, _storage, gpus, _banner):
        seen["gpus"] = gpus
        return "rendered"

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "Live", DummyLive)

    result = app.run(
        collector=lambda: {"tick": 1},
        renderer=renderer,
        sleep=fake_sleep,
        storage_collector=lambda: (),
        gpu_collector=lambda: ("gpu",),
    )

    assert result == 0
    assert seen["gpus"] == ("gpu",)


def test_run_defaults_to_the_real_gpu_collector():
    import inspect

    assert inspect.signature(app.run).parameters["gpu_collector"].default is app.collect_gpus


from datetime import datetime

from harbor_console.checks import STATE_FAILED, Check
from harbor_console.verdict import Verdict


def test_run_reads_the_verdict_every_tick_and_passes_a_banner(monkeypatch):
    seen = {}
    now = datetime(2026, 10, 9, 17, 21, 46)
    broken = Verdict(now, "h", (Check("docker", STATE_FAILED, "could not be read"),))

    def renderer(_metrics, _storage, _gpus, banner):
        seen["banner"] = banner
        return "rendered"

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "Live", DummyLive)

    app.run(
        collector=lambda: {"tick": 1},
        renderer=renderer,
        sleep=fake_sleep,
        storage_collector=lambda: (),
        gpu_collector=lambda: (),
        verdict_reader=lambda: broken,
        clock=lambda: now,
    )

    assert seen["banner"] is not None


def test_run_passes_no_banner_when_healthy(monkeypatch):
    seen = {}

    def renderer(_metrics, _storage, _gpus, banner):
        seen["banner"] = banner
        return "rendered"

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "Live", DummyLive)

    app.run(
        collector=lambda: {"tick": 1},
        renderer=renderer,
        sleep=fake_sleep,
        storage_collector=lambda: (),
        gpu_collector=lambda: (),
        verdict_reader=lambda: None,
        clock=lambda: datetime(2026, 10, 9, 17, 21, 46),
    )

    assert seen["banner"] is None


def test_run_defaults_to_the_real_verdict_reader():
    import inspect

    from harbor_console.verdict import read_verdict

    assert inspect.signature(app.run).parameters["verdict_reader"].default is read_verdict
