import inspect
from datetime import date, datetime
from http.server import ThreadingHTTPServer

import pytest

from harbor_console import web, webapp
from harbor_console.docker import DOCKER_UNAVAILABLE, Container
from harbor_console.listening import Listener
from harbor_console.ports.ledger import Lease, LedgerError
from harbor_console.probe import Health
from harbor_console.reconcile import RUNNING_NOT_DECLARED
from harbor_console.snapshot import Snapshot
from harbor_console.tailnet import TailnetUnavailable

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

WEB_LEASE = Lease("harbor-console", "web", "hpz440", "0.0.0.0", 8090, date(2026, 9, 1))
GTE_LEASE = Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 9, 1))


def test_own_port_comes_from_the_ledger():
    assert webapp.own_port([GTE_LEASE, WEB_LEASE]) == 8090


def test_own_port_missing_is_an_error():
    with pytest.raises(webapp.NotDeclared):
        webapp.own_port([GTE_LEASE])


def test_collect_snapshot_gathers_every_source():
    snapshot = webapp.collect_snapshot(
        leases=(GTE_LEASE,),
        now=datetime(2026, 9, 2, 14, 2, 11),
        collector=lambda: METRICS,
        listeners=lambda: (Listener("0.0.0.0", 8080, None),),
        containers=lambda: (Container("gte", (("0.0.0.0", 8080),)),),
        prober=lambda host, port: Health(True, "ok", "fine", (), None),
    )

    assert snapshot.metrics == METRICS
    assert snapshot.docker_available is True
    assert snapshot.health[("gte", "console")].up is True
    assert snapshot.drift == ()
    assert snapshot.ledger_error is None


def test_collect_snapshot_marks_docker_unavailable():
    snapshot = webapp.collect_snapshot(
        leases=(GTE_LEASE,),
        now=datetime(2026, 9, 2, 14, 2, 11),
        collector=lambda: METRICS,
        listeners=lambda: (Listener("0.0.0.0", 8080, None),),
        containers=lambda: DOCKER_UNAVAILABLE,
        prober=lambda host, port: Health(True, None, None, (), None),
    )

    assert snapshot.docker_available is False
    assert snapshot.containers == ()


def test_collect_snapshot_reconciles_against_the_host_it_serves():
    """The host is this machine, so another host's lease covers nothing here."""
    elsewhere = Lease("gte", "console", "other-box", "0.0.0.0", 8080, date(2026, 9, 1))

    snapshot = webapp.collect_snapshot(
        leases=(elsewhere,),
        now=datetime(2026, 9, 2, 14, 2, 11),
        collector=lambda: METRICS,
        listeners=lambda: (),
        containers=lambda: (Container("gte", (("0.0.0.0", 8080),)),),
        prober=lambda host, port: Health(False, None, None, (), None),
    )

    assert [item.kind for item in snapshot.drift] == [RUNNING_NOT_DECLARED]


def test_collect_snapshot_hands_reconcile_a_concrete_sequence():
    """find_drift walks the leases twice; a generator would empty itself."""
    snapshot = webapp.collect_snapshot(
        leases=iter((GTE_LEASE,)),
        now=datetime(2026, 9, 2, 14, 2, 11),
        collector=lambda: METRICS,
        listeners=lambda: (Listener("0.0.0.0", 8080, None),),
        containers=lambda: (Container("gte", (("0.0.0.0", 8080),)),),
        prober=lambda host, port: Health(True, None, None, (), None),
    )

    assert snapshot.leases == (GTE_LEASE,)
    assert snapshot.drift == ()


def test_probe_loop_publishes_a_snapshot_then_exits_cleanly():
    holder = webapp.SnapshotHolder(
        Snapshot(collected=datetime(2026, 1, 1), metrics=METRICS)
    )
    calls = {"count": 0}

    def collect():
        calls["count"] += 1
        return Snapshot(
            collected=datetime(2026, 9, 2), metrics=METRICS, leases=(GTE_LEASE,)
        )

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    webapp.probe_loop(holder, collect=collect, sleep=fake_sleep, interval=30.0)

    assert calls["count"] == 1
    assert holder.get().leases == (GTE_LEASE,)


def test_probe_loop_keeps_the_last_snapshot_when_collection_fails():
    good = Snapshot(collected=datetime(2026, 1, 1), metrics=METRICS, leases=(GTE_LEASE,))
    holder = webapp.SnapshotHolder(good)

    def collect():
        raise LedgerError("services.toml: boom")

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    webapp.probe_loop(holder, collect=collect, sleep=fake_sleep, interval=30.0)

    assert holder.get().leases == (GTE_LEASE,)
    assert holder.get().ledger_error is not None


def test_probe_loop_survives_a_failure_that_is_not_the_ledger():
    """Any collector can fail; the page must not become the thing that is wrong."""
    good = Snapshot(collected=datetime(2026, 1, 1), metrics=METRICS, leases=(GTE_LEASE,))
    holder = webapp.SnapshotHolder(good)

    def collect():
        raise RuntimeError("psutil fell over")

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    webapp.probe_loop(holder, collect=collect, sleep=fake_sleep, interval=30.0)

    assert holder.get().leases == (GTE_LEASE,)
    assert "psutil fell over" in (holder.get().ledger_error or "")


def test_probe_loop_clears_a_stale_reason_on_the_next_good_cycle():
    stale = Snapshot(
        collected=datetime(2026, 1, 1),
        metrics=METRICS,
        ledger_error="services.toml: boom",
    )
    holder = webapp.SnapshotHolder(stale)

    def collect():
        return Snapshot(collected=datetime(2026, 9, 2), metrics=METRICS)

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    webapp.probe_loop(holder, collect=collect, sleep=fake_sleep, interval=30.0)

    assert holder.get().ledger_error is None


def test_the_starting_snapshot_renders_before_the_first_cycle():
    """A request landing before the prober's first cycle still gets a page."""
    snapshot = webapp.starting_snapshot(
        "hpz440", (WEB_LEASE,), datetime(2026, 9, 2, 14, 2, 11)
    )

    assert b"hpz440" in web.render_page(snapshot)


def test_main_binds_the_tailnet_address_and_the_leased_port(monkeypatch):
    bound = {}

    class FakeServer:
        def __init__(self, address, handler):
            bound["address"] = address

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            bound["closed"] = True

    monkeypatch.setattr(webapp, "tailscale_address", lambda: "100.69.239.123")
    monkeypatch.setattr(webapp, "load_leases", lambda _path: [WEB_LEASE, GTE_LEASE])

    result = webapp.main(server_factory=FakeServer, start_prober=lambda _holder: None)

    assert result == 0
    assert bound["address"] == ("100.69.239.123", 8090)
    assert bound["closed"] is True


def test_main_starts_the_prober_before_it_serves(monkeypatch):
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
    monkeypatch.setattr(webapp, "load_leases", lambda _path: [WEB_LEASE])

    def start(_holder):
        order.append("probing")

    assert webapp.main(server_factory=FakeServer, start_prober=start) == 0
    assert order == ["probing", "bound", "served", "closed"]


def test_main_reports_a_bind_that_fails(monkeypatch):
    """The leased port already taken is a refusal, not a traceback."""
    monkeypatch.setattr(webapp, "tailscale_address", lambda: "100.69.239.123")
    monkeypatch.setattr(webapp, "load_leases", lambda _path: [WEB_LEASE])

    def factory(_address, _handler):
        raise OSError("address already in use")

    result = webapp.main(server_factory=factory, start_prober=lambda _h: None)

    assert result != 0


def test_the_default_server_is_threading():
    """HTTP/1.1 keep-alive plus one thread would let one client hold the page."""
    default = inspect.signature(webapp.main).parameters["server_factory"].default

    assert default is ThreadingHTTPServer


def test_main_refuses_to_start_without_a_tailnet_address(monkeypatch):
    def boom():
        raise TailnetUnavailable("tailscaled is not up")

    monkeypatch.setattr(webapp, "tailscale_address", boom)
    monkeypatch.setattr(webapp, "load_leases", lambda _path: [WEB_LEASE])

    called = {"served": False}

    def factory(_address, _handler):
        called["served"] = True

    result = webapp.main(server_factory=factory, start_prober=lambda _holder: None)

    assert result != 0
    assert called["served"] is False


def test_main_refuses_to_start_when_the_ledger_is_unreadable(monkeypatch):
    monkeypatch.setattr(webapp, "tailscale_address", lambda: "100.69.239.123")

    def boom(_path):
        raise LedgerError("services.toml: unreadable")

    monkeypatch.setattr(webapp, "load_leases", boom)

    result = webapp.main(server_factory=lambda *a: None, start_prober=lambda _h: None)

    assert result != 0


def test_main_refuses_to_start_when_its_own_lease_is_missing(monkeypatch):
    monkeypatch.setattr(webapp, "tailscale_address", lambda: "100.69.239.123")
    monkeypatch.setattr(webapp, "load_leases", lambda _path: [GTE_LEASE])

    result = webapp.main(server_factory=lambda *a: None, start_prober=lambda _h: None)

    assert result != 0


def test_the_ledger_path_is_the_one_the_allocator_writes():
    assert webapp.LEDGER_PATH.name == "services.toml"
    assert webapp.LEDGER_PATH.exists()
