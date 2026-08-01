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

    def renderer(metrics):
        return f"render-{metrics['tick']}"

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "Live", DummyLive)

    result = app.run(
        refresh_interval=1.0,
        collector=collector,
        renderer=renderer,
        sleep=fake_sleep,
    )

    assert result == 0
    assert calls["count"] == 1
