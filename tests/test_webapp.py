import inspect
from datetime import datetime
from http.server import ThreadingHTTPServer

from harbor_console import webapp
from harbor_console.directory import KIND_HTTP, ROUTE_ERROR, UNDECLARED_CONTAINER
from harbor_console.docker import DOCKER_UNAVAILABLE, Container
from harbor_console.listening import LISTENING_UNAVAILABLE, Listener
from harbor_console.probe import Health
from harbor_console.snapshot import Snapshot
from harbor_console.tailnet import TailnetUnavailable
from harbor_console.traefik import TRAEFIK_UNAVAILABLE, Router

METRICS = {
    "hostname": "hpz440",
    "uptime": "1d 00:00:00",
    "cpu_utilization": 1.0,
    "memory_utilization": 2.0,
    "disk_utilization": 3.0,
    "ipv4_address": "10.0.0.7",
    "docker_container_count": 1,
    "current_datetime": "2026-09-02 14:02:11",
}
NOW = datetime(2026, 9, 2, 14, 2, 11)
HOST = "parksmart.hpz440.ohr3023.org"
PARKSMART = Container(
    "parksmart-parksmart-1",
    (),
    {"traefik.enable": "true", "traefik.http.routers.parksmart.rule": f"Host(`{HOST}`)"},
    frozenset({"harbor"}),
)
ROUTER = Router("parksmart@docker", HOST, "parksmart", True, None)


def collect(**overrides):
    kwargs = dict(
        now=NOW,
        collector=lambda: METRICS,
        listeners=lambda: (),
        containers=lambda: (PARKSMART,),
        routers=lambda: (ROUTER,),
        prober=lambda url: Health(True, "ok", "fine", (), None),
        tailnet_address="100.69.239.123",
    )
    kwargs.update(overrides)
    return webapp.collect_snapshot(**kwargs)


def test_collect_snapshot_gathers_every_source():
    snapshot = collect()

    assert snapshot.metrics == METRICS
    assert snapshot.docker_available is True
    assert snapshot.traefik_available is True
    assert snapshot.listeners_available is True
    assert snapshot.health["parksmart"].up is True
    assert [r.name for r in snapshot.rows] == ["parksmart"]
    assert snapshot.rows[0].state == "UP"
    assert snapshot.findings == ()
    assert snapshot.probed is True
    assert snapshot.collection_error is None


def test_http_rows_are_probed_at_their_route():
    seen = []

    def prober(url):
        seen.append(url)
        return Health(True, None, None, (), None)

    collect(prober=prober)

    assert seen == [f"https://{HOST}/"]


def test_rows_without_a_host_are_not_probed():
    seen = []
    plain = Container("plain", (), {"traefik.enable": "true"})

    collect(containers=lambda: (plain,), routers=lambda: (), prober=lambda url: seen.append(url))

    assert seen == []


def test_non_http_rows_are_not_probed():
    seen = []
    mqtt = Container("mqtt", (("0.0.0.0", 1883),), {"harbor.kind": "tcp", "harbor.port": "1883"})

    collect(containers=lambda: (mqtt,), routers=lambda: (), prober=lambda url: seen.append(url))

    assert seen == []


def test_collect_snapshot_marks_docker_unavailable():
    snapshot = collect(containers=lambda: DOCKER_UNAVAILABLE)

    assert snapshot.docker_available is False
    assert snapshot.containers == ()
    assert snapshot.rows == ()
    assert snapshot.findings == ()


def test_collect_snapshot_marks_listeners_unavailable():
    # A third assertion here that no undeclared-tailnet-listener finding
    # appears would be vacuous: the `collect` fixture's container publishes
    # nothing, so no such finding would arise even with ordinary empty
    # listeners. That guard is exercised directly, with a populated
    # sentinel, in test_directory.py and test_inventory.py.
    snapshot = collect(listeners=lambda: LISTENING_UNAVAILABLE)

    assert snapshot.listeners_available is False
    assert snapshot.inventory == ()


def test_collect_snapshot_marks_traefik_unavailable():
    snapshot = collect(routers=lambda: TRAEFIK_UNAVAILABLE)

    assert snapshot.traefik_available is False
    assert snapshot.rows[0].state == "UP"
    assert all(f.kind != ROUTE_ERROR for f in snapshot.findings)


def test_collect_snapshot_reports_findings():
    snapshot = collect(containers=lambda: (PARKSMART, Container("mystery", ())))

    assert [f.kind for f in snapshot.findings] == [UNDECLARED_CONTAINER]


def test_the_pages_own_bind_is_not_a_finding():
    snapshot = collect(listeners=lambda: (Listener("100.69.239.123", webapp.WEB_PORT, None),))

    assert snapshot.findings == ()


def test_starting_snapshot_has_looked_at_nothing():
    snapshot = webapp.starting_snapshot("hpz440", NOW, tailnet_address="100.69.239.123")

    assert snapshot.probed is False
    assert snapshot.rows == ()
    assert snapshot.metrics["hostname"] == "hpz440"
    assert snapshot.tailnet_address == "100.69.239.123"


def test_probe_loop_publishes_a_snapshot_then_exits_cleanly():
    holder = webapp.SnapshotHolder(
        Snapshot(collected=datetime(2026, 1, 1), metrics=METRICS)
    )
    calls = {"count": 0}

    def collect_fn():
        calls["count"] += 1
        return Snapshot(collected=datetime(2026, 9, 2), metrics=METRICS)

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    webapp.probe_loop(holder, collect=collect_fn, sleep=fake_sleep, interval=30.0)

    assert calls["count"] == 1
    assert holder.get().collected == datetime(2026, 9, 2)


def test_probe_loop_keeps_the_last_snapshot_when_collection_fails():
    good = Snapshot(collected=datetime(2026, 1, 1), metrics=METRICS)
    holder = webapp.SnapshotHolder(good)

    def collect_fn():
        raise RuntimeError("psutil fell over")

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    webapp.probe_loop(holder, collect=collect_fn, sleep=fake_sleep, interval=30.0)

    assert holder.get().collected == datetime(2026, 1, 1)
    assert "psutil fell over" in (holder.get().collection_error or "")


def test_probe_loop_clears_a_stale_reason_on_the_next_good_cycle():
    stale = Snapshot(
        collected=datetime(2026, 1, 1),
        metrics=METRICS,
        collection_error="psutil fell over",
    )
    holder = webapp.SnapshotHolder(stale)

    def collect_fn():
        return Snapshot(collected=datetime(2026, 9, 2), metrics=METRICS)

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    webapp.probe_loop(holder, collect=collect_fn, sleep=fake_sleep, interval=30.0)

    assert holder.get().collection_error is None


def test_probe_loop_sleeps_for_the_interval_it_was_given():
    holder = webapp.SnapshotHolder(
        Snapshot(collected=datetime(2026, 1, 1), metrics=METRICS)
    )
    slept = []

    def collect_fn():
        return Snapshot(collected=datetime(2026, 9, 2), metrics=METRICS)

    def fake_sleep(interval):
        slept.append(interval)
        raise KeyboardInterrupt

    webapp.probe_loop(holder, collect=collect_fn, sleep=fake_sleep, interval=30.0)

    assert slept == [30.0]


def test_the_default_probe_interval_is_not_a_busy_spin():
    assert webapp.PROBE_INTERVAL_SECONDS >= 1.0
    assert (
        inspect.signature(webapp.probe_loop).parameters["interval"].default
        == webapp.PROBE_INTERVAL_SECONDS
    )


def test_main_refuses_to_start_without_a_tailnet_address(monkeypatch, capsys):
    def boom():
        raise TailnetUnavailable("no tailnet")

    monkeypatch.setattr(webapp, "tailscale_address", boom)

    called = {"served": False}

    def factory(_address, _handler):
        called["served"] = True

    result = webapp.main(server_factory=factory, start_prober=lambda _holder, _addr: None)

    assert result != 0
    assert called["served"] is False
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "no tailnet" in err


def test_main_reports_a_bind_that_fails(monkeypatch, capsys):
    """A port already in use is a refusal, not a traceback."""
    monkeypatch.setattr(webapp, "tailscale_address", lambda: "100.69.239.123")

    def factory(_address, _handler):
        raise OSError("address in use")

    result = webapp.main(server_factory=factory, start_prober=lambda _h, _addr: None)

    assert result != 0
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "100.69.239.123:8100" in err
    assert "address in use" in err


def test_a_failed_bind_starts_no_prober(monkeypatch):
    monkeypatch.setattr(webapp, "tailscale_address", lambda: "100.69.239.123")

    started = {"probing": False}

    def factory(_address, _handler):
        raise OSError("address already in use")

    def start(_holder, _addr):
        started["probing"] = True

    assert webapp.main(server_factory=factory, start_prober=start) != 0
    assert started["probing"] is False


def test_main_starts_the_prober_after_the_bind_and_before_it_serves(monkeypatch):
    order = []

    class FakeServer:
        def __init__(self, _address, _handler):
            order.append("bound")

        def serve_forever(self):
            order.append("served")
            raise KeyboardInterrupt

        def server_close(self):
            order.append("closed")

    monkeypatch.setattr(webapp, "tailscale_address", lambda: "100.69.239.123")

    def start(_holder, _addr):
        order.append("probing")

    assert webapp.main(server_factory=FakeServer, start_prober=start) == 0
    assert order == ["bound", "probing", "served", "closed"]


def test_main_binds_the_tailnet_address_and_web_port(monkeypatch):
    bound = {}

    class FakeServer:
        def __init__(self, address, _handler):
            bound["address"] = address

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            bound["closed"] = True

    monkeypatch.setattr(webapp, "tailscale_address", lambda: "100.69.239.123")

    result = webapp.main(server_factory=FakeServer, start_prober=lambda _holder, _addr: None)

    assert result == 0
    assert bound["address"] == ("100.69.239.123", webapp.WEB_PORT)
    assert bound["closed"] is True


def test_the_default_server_is_threading():
    default = inspect.signature(webapp.main).parameters["server_factory"].default

    assert default is ThreadingHTTPServer


def test_read_traefik_credentials_reads_the_password_file(tmp_path):
    path = tmp_path / "dashboard-password"
    path.write_text("s3cret\n", encoding="utf-8")

    assert webapp.read_traefik_credentials(path) == (webapp.TRAEFIK_DASHBOARD_USER, "s3cret")


def test_read_traefik_credentials_strips_surrounding_whitespace(tmp_path):
    path = tmp_path / "dashboard-password"
    path.write_text("  s3cret  \n", encoding="utf-8")

    assert webapp.read_traefik_credentials(path) == (webapp.TRAEFIK_DASHBOARD_USER, "s3cret")


def test_read_traefik_credentials_degrades_when_the_file_is_missing(tmp_path):
    assert webapp.read_traefik_credentials(tmp_path / "nope") is None


def test_read_traefik_credentials_degrades_on_an_empty_file(tmp_path):
    path = tmp_path / "dashboard-password"
    path.write_text("\n", encoding="utf-8")

    assert webapp.read_traefik_credentials(path) is None


def test_collect_snapshot_builds_the_listening_inventory():
    snapshot = collect(listeners=lambda: (Listener("0.0.0.0", 22, None),))

    assert [(e.addr, e.port, e.reach, e.accounted) for e in snapshot.inventory] == [
        ("0.0.0.0", 22, "LAN + tailnet", "")
    ]


def test_the_inventory_knows_the_pages_own_bind():
    snapshot = collect(listeners=lambda: (Listener("100.69.239.123", 8100, None),))

    assert snapshot.inventory[0].accounted == "harbor-console-web"


def test_the_inventory_is_unknown_when_docker_is_unavailable():
    snapshot = collect(
        listeners=lambda: (Listener("0.0.0.0", 1883, None),),
        containers=lambda: DOCKER_UNAVAILABLE,
    )

    assert snapshot.inventory[0].accounted == "unknown"
