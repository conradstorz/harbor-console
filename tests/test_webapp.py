import inspect
import os
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


def test_main_starts_the_prober_after_the_bind_and_before_it_serves(monkeypatch):
    """The bind comes first, then the prober, then serving.

    Starting the prober first made a refusal expensive: a bind that fails --
    the leased port already taken -- would still have fired a whole collection
    cycle on its way out, `docker ps` plus an HTTP probe of every leased port,
    and `RestartSec=2` repeats that every two seconds for as long as the port
    stays taken. It also contradicted `starting_snapshot`'s own docstring,
    which promises the server binds before any collector runs.
    """
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
    assert order == ["bound", "probing", "served", "closed"]
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


def test_a_failed_bind_starts_no_prober(monkeypatch):
    """A refusal must cost nothing but the refusal.

    The prober used to start before the bind, so a process that could not hold
    its port still ran a full cycle -- `docker ps` and an HTTP probe of every
    leased port -- before exiting. `RestartSec=2` turns that into a collection
    storm against every service on the host, every two seconds, for as long as
    the port stays taken.
    """
    monkeypatch.setattr(webapp, "tailscale_address", lambda: "100.69.239.123")
    monkeypatch.setattr(webapp, "load_leases", lambda _path: [WEB_LEASE])

    started = {"probing": False}

    def factory(_address, _handler):
        raise OSError("address already in use")

    def start(_holder, _host):
        started["probing"] = True

    assert webapp.main(server_factory=factory, start_prober=start) != 0
    assert started["probing"] is False


def test_collect_snapshot_publishes_the_listeners_it_found():
    """The listener list is the payload `/ports.json` is built from.

    Dropping it on the floor here serves `"listening": []` with a 200: the
    allocator's `fetch_live` reads that as a complete, verified-empty host and
    grants ports that are already in use -- the same failure the 503 gate
    exists to prevent, arriving through the other door. Nothing else in the
    suite asserts `snapshot.listeners`.
    """
    found = (Listener("0.0.0.0", 8080, None), Listener("127.0.0.1", 5432, 42))

    snapshot = webapp.collect_snapshot(
        leases=(GTE_LEASE,),
        host="hpz440",
        now=datetime(2026, 9, 2, 14, 2, 11),
        collector=lambda: METRICS,
        listeners=lambda: found,
        containers=lambda: (Container("gte", (("0.0.0.0", 8080),)),),
        prober=lambda host, port: Health(True, None, None, (), None),
    )

    assert snapshot.listeners == found
    # And it survives all the way into the body the allocator reads.
    payload = web.ports_payload(snapshot)
    assert {(entry["addr"], entry["port"]) for entry in payload["listening"]} == {
        ("0.0.0.0", 8080),
        ("127.0.0.1", 5432),
    }


def test_collect_snapshot_records_when_the_ledger_was_last_written():
    """The staleness indicator comes from the injected collector, not a live
    stat call inside collect_snapshot itself."""
    written = datetime(2026, 9, 1, 22, 20, 0)

    snapshot = webapp.collect_snapshot(
        leases=(GTE_LEASE,),
        host="hpz440",
        now=datetime(2026, 9, 2, 14, 2, 11),
        collector=lambda: METRICS,
        listeners=lambda: (),
        containers=lambda: (Container("gte", (("0.0.0.0", 8080),)),),
        prober=lambda host, port: Health(True, None, None, (), None),
        ledger_mtime=lambda: written,
    )

    assert snapshot.ledger_written == written


def test_read_ledger_mtime_returns_the_files_modification_time(tmp_path):
    ledger = tmp_path / "services.toml"
    ledger.write_text("", encoding="utf-8")
    stamp = datetime(2026, 9, 1, 22, 20, 0).timestamp()
    os.utime(ledger, (stamp, stamp))

    result = webapp.read_ledger_mtime(ledger)

    assert result == datetime.fromtimestamp(stamp)


def test_read_ledger_mtime_degrades_when_the_ledger_is_missing(tmp_path):
    """Every collector in this project degrades on a hostile environment
    rather than raising -- a missing or unreadable ledger yields no
    timestamp, not a traceback."""
    missing = tmp_path / "does-not-exist.toml"

    assert webapp.read_ledger_mtime(missing) is None


def test_probe_loop_sleeps_for_the_interval_it_was_given():
    """A busy-spin prober is invisible to a fake sleep that ignores its argument.

    `sleep(0)` between cycles would hammer `docker ps` and every service on the
    host at full CPU, and every other test here passes a `fake_sleep` that
    discards what it is handed. Assert the value actually passed.
    """
    holder = webapp.SnapshotHolder(
        Snapshot(collected=datetime(2026, 1, 1), metrics=METRICS)
    )
    slept = []

    def collect():
        return Snapshot(collected=datetime(2026, 9, 2), metrics=METRICS)

    def fake_sleep(interval):
        slept.append(interval)
        raise KeyboardInterrupt

    webapp.probe_loop(holder, collect=collect, sleep=fake_sleep, interval=30.0)

    assert slept == [30.0]


def test_the_default_probe_interval_is_not_a_busy_spin():
    """The default the real prober thread runs on, asserted where it is read."""
    assert webapp.PROBE_INTERVAL_SECONDS >= 1.0
    assert (
        inspect.signature(webapp.probe_loop).parameters["interval"].default
        == webapp.PROBE_INTERVAL_SECONDS
    )


def test_every_refusal_says_why_on_stderr(monkeypatch, capsys):
    """The refusal design's whole payoff is a line in journald.

    Four refusals plus the bind, all silent-passing until now: no test in the
    suite read stderr, so deleting every message left 289 tests green and an
    operator with nothing but a non-zero exit code.
    """
    elsewhere = Lease("harbor-console", "web", "nas", "0.0.0.0", 8090, date(2026, 9, 1))

    def no_tailnet():
        raise TailnetUnavailable("tailscaled is not up")

    def bad_ledger(_path):
        raise LedgerError("services.toml: unreadable")

    def bad_bind(_address, _handler):
        raise OSError("address already in use")

    cases = [
        (no_tailnet, lambda _path: [WEB_LEASE], lambda *a: None, "tailscaled is not up"),
        (lambda: "100.69.239.123", bad_ledger, lambda *a: None, "services.toml: unreadable"),
        (lambda: "100.69.239.123", lambda _path: [GTE_LEASE], lambda *a: None, "harbor-console/web"),
        (
            lambda: "100.69.239.123",
            lambda _path: [WEB_LEASE, elsewhere],
            lambda *a: None,
            "nas",
        ),
        (
            lambda: "100.69.239.123",
            lambda _path: [WEB_LEASE],
            bad_bind,
            "100.69.239.123:8090",
        ),
    ]

    for address, leases, factory, expected in cases:
        monkeypatch.setattr(webapp, "tailscale_address", address)
        monkeypatch.setattr(webapp, "load_leases", leases)
        capsys.readouterr()

        result = webapp.main(server_factory=factory, start_prober=lambda _h, _host: None)

        captured = capsys.readouterr()
        assert result != 0
        assert expected in captured.err, f"{expected!r} missing from {captured.err!r}"
        assert captured.err.startswith("error:")
