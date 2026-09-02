import inspect
from datetime import date, datetime
from http.server import ThreadingHTTPServer

import pytest

from harbor_console import web, webapp
from harbor_console.docker import DOCKER_UNAVAILABLE, Container
from harbor_console.listening import Listener
from harbor_console.ports.ledger import Lease, LedgerError
from harbor_console.probe import Health
from harbor_console.reconcile import DECLARED_NOT_RUNNING, RUNNING_NOT_DECLARED
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
        host="hpz440",
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
    assert snapshot.collection_error is None


def test_collect_snapshot_marks_docker_unavailable():
    snapshot = webapp.collect_snapshot(
        leases=(GTE_LEASE,),
        host="hpz440",
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
        host="hpz440",
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
        host="hpz440",
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
    assert holder.get().collection_error is not None


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
    assert "psutil fell over" in (holder.get().collection_error or "")


def test_probe_loop_clears_a_stale_reason_on_the_next_good_cycle():
    stale = Snapshot(
        collected=datetime(2026, 1, 1),
        metrics=METRICS,
        collection_error="services.toml: boom",
    )
    holder = webapp.SnapshotHolder(stale)

    def collect():
        return Snapshot(collected=datetime(2026, 9, 2), metrics=METRICS)

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    webapp.probe_loop(holder, collect=collect, sleep=fake_sleep, interval=30.0)

    assert holder.get().collection_error is None


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

    result = webapp.main(server_factory=FakeServer, start_prober=lambda _holder, _host: None)

    assert result == 0
    assert bound["address"] == ("100.69.239.123", 8090)
    assert bound["closed"] is True


def test_main_starts_the_prober_before_it_serves(monkeypatch):
    order = []
    given = {}

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

    def start(_holder, host):
        order.append("probing")
        given["host"] = host

    assert webapp.main(server_factory=FakeServer, start_prober=start) == 0
    assert order == ["probing", "bound", "served", "closed"]
    # The prober reconciles against the host this process bound for, which
    # is the lease's host and never the OS's idea of it.
    assert given["host"] == WEB_LEASE.host


def test_main_reports_a_bind_that_fails(monkeypatch):
    """The leased port already taken is a refusal, not a traceback."""
    monkeypatch.setattr(webapp, "tailscale_address", lambda: "100.69.239.123")
    monkeypatch.setattr(webapp, "load_leases", lambda _path: [WEB_LEASE])

    def factory(_address, _handler):
        raise OSError("address already in use")

    result = webapp.main(server_factory=factory, start_prober=lambda _h, _host: None)

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

    result = webapp.main(server_factory=factory, start_prober=lambda _holder, _host: None)

    assert result != 0
    assert called["served"] is False


def test_main_refuses_to_start_when_the_ledger_is_unreadable(monkeypatch):
    monkeypatch.setattr(webapp, "tailscale_address", lambda: "100.69.239.123")

    def boom(_path):
        raise LedgerError("services.toml: unreadable")

    monkeypatch.setattr(webapp, "load_leases", boom)

    result = webapp.main(server_factory=lambda *a: None, start_prober=lambda _h, _host: None)

    assert result != 0


def test_main_refuses_to_start_when_its_own_lease_is_missing(monkeypatch):
    monkeypatch.setattr(webapp, "tailscale_address", lambda: "100.69.239.123")
    monkeypatch.setattr(webapp, "load_leases", lambda _path: [GTE_LEASE])

    result = webapp.main(server_factory=lambda *a: None, start_prober=lambda _h, _host: None)

    assert result != 0


def test_the_ledger_path_is_the_one_the_allocator_writes():
    assert webapp.LEDGER_PATH.name == "services.toml"
    assert webapp.LEDGER_PATH.exists()


def test_collect_snapshot_uses_the_host_it_was_given_not_the_os_hostname():
    """The ledger's host is hand-authored; the OS need not agree with it.

    `gethostname()` says `hpz440.lan` while the ledger says `hpz440`. Deriving
    the host from the collected metrics emptied this host's share of the
    ledger: `find_drift` keeps only `lease.host == host`, so the dead service
    below -- nothing listening on a leased port, the finding this page exists
    to make -- disappeared, and the container correctly serving that lease was
    reported as undeclared instead.
    """
    metrics = dict(METRICS, hostname="hpz440.lan")

    snapshot = webapp.collect_snapshot(
        leases=(GTE_LEASE,),
        host="hpz440",
        now=datetime(2026, 9, 2, 14, 2, 11),
        collector=lambda: metrics,
        listeners=lambda: (),
        containers=lambda: (Container("gte", (("0.0.0.0", 8080),)),),
        prober=lambda host, port: Health(False, None, None, (), None),
    )

    assert [item.kind for item in snapshot.drift] == [DECLARED_NOT_RUNNING]
    # The page and /ports.json name the host the ledger names, so the heading
    # before the first cycle and the one after it cannot disagree.
    assert snapshot.metrics["hostname"] == "hpz440"
    # The collector's dict is not ours to edit.
    assert metrics["hostname"] == "hpz440.lan"


def test_own_lease_refuses_when_two_hosts_declare_this_service():
    """The ledger is fleet-wide, so the first match may be another machine's.

    Binding a port this host does not hold is the exact collision the ledger
    exists to prevent. There is no hostname tiebreak on purpose.
    """
    elsewhere = Lease("harbor-console", "web", "nas", "0.0.0.0", 8090, date(2026, 9, 1))

    with pytest.raises(webapp.AmbiguousDeclaration) as caught:
        webapp.own_lease([WEB_LEASE, elsewhere])

    message = str(caught.value)
    assert "hpz440" in message
    assert "nas" in message


def test_own_lease_ambiguity_names_addr_and_port_not_just_a_repeated_host():
    """A hand-edited ledger can declare the same service twice on one host,
    on different addresses or ports. Naming only `lease.host` reads as the
    same string twice -- "hpz440, hpz440" -- and gives the operator nothing
    to find in the file. addr:port must distinguish each candidate.
    """
    first = Lease("harbor-console", "web", "hpz440", "0.0.0.0", 8090, date(2026, 9, 1))
    second = Lease("harbor-console", "web", "hpz440", "127.0.0.1", 8091, date(2026, 9, 1))

    with pytest.raises(webapp.AmbiguousDeclaration) as caught:
        webapp.own_lease([first, second])

    message = str(caught.value)
    assert "0.0.0.0:8090" in message
    assert "127.0.0.1:8091" in message


def test_main_refuses_to_start_when_its_own_lease_is_ambiguous(monkeypatch):
    """An ambiguous identity is a refusal, like the other three, before any bind."""
    elsewhere = Lease("harbor-console", "web", "nas", "0.0.0.0", 8090, date(2026, 9, 1))
    monkeypatch.setattr(webapp, "tailscale_address", lambda: "100.69.239.123")
    monkeypatch.setattr(webapp, "load_leases", lambda _path: [WEB_LEASE, elsewhere])

    called = {"served": False}

    def factory(_address, _handler):
        called["served"] = True

    result = webapp.main(server_factory=factory, start_prober=lambda _h, _host: None)

    assert result != 0
    assert called["served"] is False


def test_a_collector_failure_is_not_blamed_on_the_ledger():
    """psutil raising must not send the operator to read services.toml."""
    good = Snapshot(collected=datetime(2026, 1, 1), metrics=METRICS, leases=(GTE_LEASE,))
    holder = webapp.SnapshotHolder(good)

    def collect():
        raise RuntimeError("psutil fell over")

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    webapp.probe_loop(holder, collect=collect, sleep=fake_sleep, interval=30.0)
    html = web.render_page(holder.get()).decode()

    assert "psutil fell over" in html
    assert "ledger" not in html.lower()


def test_a_ledger_failure_is_still_named_as_one():
    """The common case must stay legible after the field stopped naming it."""
    good = Snapshot(collected=datetime(2026, 1, 1), metrics=METRICS, leases=(GTE_LEASE,))
    holder = webapp.SnapshotHolder(good)

    def collect():
        raise LedgerError("services.toml: boom")

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    webapp.probe_loop(holder, collect=collect, sleep=fake_sleep, interval=30.0)
    html = web.render_page(holder.get()).decode()

    assert "ledger" in html.lower()
    assert "services.toml: boom" in html


def test_the_starting_snapshot_claims_no_clean_bill_of_health():
    """Nothing has been probed, so nothing is UP, nothing is DOWN, nothing is clean."""
    snapshot = webapp.starting_snapshot(
        "hpz440", (WEB_LEASE,), datetime(2026, 9, 2, 14, 2, 11)
    )

    assert snapshot.probed is False

    html = web.render_page(snapshot).decode()

    assert "DOWN" not in html
    assert "no drift" not in html.lower()
    # Said in both places that would otherwise assert a state: services and drift.
    assert html.lower().count("nothing has been collected yet") == 2
