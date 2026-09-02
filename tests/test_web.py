import json
import urllib.error
from datetime import date, datetime
from html import escape
from io import BytesIO

import pytest

from harbor_console.docker import Container
from harbor_console.listening import Listener
from harbor_console.ports.ledger import Lease
from harbor_console.ports.live import LiveUnavailable, fetch_live
from harbor_console.probe import Detail, Health
from harbor_console.snapshot import Drift, Snapshot
from harbor_console.web import make_handler, ports_payload, render_page

#: A string that is hostile in every free-text field the page interpolates:
#: it breaks out of an attribute, closes a tag, and opens a <script>.
PAYLOAD = "\"><script>alert(1)</script><a b='"

METRICS = {
    "hostname": "hpz440",
    "uptime": "1d 00:00:00",
    "cpu_utilization": 12.5,
    "memory_utilization": 45.0,
    "disk_utilization": 78.0,
    "ipv4_address": "10.0.0.7",
    "docker_container_count": 3,
    "current_datetime": "2026-09-02 14:02:11",
}


def snapshot(**overrides):
    base = dict(
        collected=datetime(2026, 9, 2, 14, 2, 11),
        metrics=METRICS,
        leases=(Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 9, 1)),),
        listeners=(Listener("0.0.0.0", 8080, None),),
        containers=(Container("gte", (("0.0.0.0", 8080),)),),
        docker_available=True,
        health={("gte", "console"): Health(True, "ok", "3 queued", (Detail("queue", "3"),), None)},
        drift=(),
        collection_error=None,
        # These render tests describe a page that has been collected; the
        # unprobed page is its own case, below.
        probed=True,
    )
    base.update(overrides)
    return Snapshot(**base)


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self._body


def test_ports_payload_round_trips_through_the_allocators_reader():
    payload = ports_payload(
        snapshot(
            listeners=(
                Listener("0.0.0.0", 8080, None),
                Listener("127.0.0.1", 5432, None),
                Listener("0.0.0.0", 22, None),
            ),
            containers=(
                Container("gte", (("0.0.0.0", 8080),)),
                Container("shared-postgres", (("127.0.0.1", 5432),)),
            ),
        )
    )
    body = json.dumps(payload).encode()

    live = fetch_live("http://x/ports.json", opener=lambda _u, timeout: FakeResponse(body))

    assert live.host == "hpz440"
    assert live.complete is True
    assert live.is_listening("0.0.0.0", 8080) is True
    assert live.is_listening("127.0.0.1", 5432) is True
    assert live.container_on(8080) == "gte"
    assert live.container_on(22) is None


def test_ports_payload_ports_are_real_integers():
    payload = ports_payload(snapshot())

    for entry in payload["listening"]:
        assert type(entry["port"]) is int


def test_ports_payload_attributes_a_wildcard_listener_to_a_specific_publish():
    """A listener on 0.0.0.0 must match a container published on a specific
    address, using the same `addrs_overlap` rule `ports/live.py` applies --
    not a narrower hand-rolled check that only matches the reverse direction.
    """
    payload = ports_payload(
        snapshot(
            listeners=(Listener("0.0.0.0", 8080, None),),
            containers=(
                Container("web", (("127.0.0.1", 8080),)),
                Container("also-web", (("192.168.1.5", 8080),)),
            ),
        )
    )

    entry = payload["listening"][0]
    assert entry["addr"] == "0.0.0.0"
    assert entry["container"] is not None


def test_page_shows_host_metrics_and_the_service():
    html = render_page(snapshot()).decode()

    assert "hpz440" in html
    assert "8080" in html
    assert "UP" in html
    assert "3 queued" in html
    assert "queue" in html


def test_page_shows_a_down_service():
    health = {("gte", "console"): Health(False, None, None, (), None)}
    html = render_page(snapshot(health=health, listeners=())).decode()

    assert "DOWN" in html


def test_page_shows_drift():
    drift = (Drift("declared-not-running", "gte/console leases 0.0.0.0:8080, nothing is listening"),)
    html = render_page(snapshot(drift=drift)).decode()

    assert "nothing is listening" in html


def test_page_says_so_when_there_is_no_drift():
    html = render_page(snapshot(drift=())).decode()

    assert "no drift" in html.lower()


def test_page_does_not_call_an_unprobed_fleet_clean():
    """Before the first cycle nothing is known, and the page must say so."""
    html = render_page(snapshot(probed=False, health={}, drift=())).decode()

    assert "DOWN" not in html
    assert "no drift" not in html.lower()
    assert "unknown" in html.lower()


def test_banner_does_not_claim_a_last_good_page_before_the_first_cycle():
    """When the very first collection cycle fails, there is no last good
    page to fall back to -- only the starting placeholder. The banner must
    say the collection failed without also claiming a page it never had.
    """
    html = render_page(
        snapshot(probed=False, health={}, drift=(), collection_error="services.toml: boom")
    ).decode()

    assert "services.toml: boom" in html
    assert "last good page" not in html.lower()


def test_page_shows_the_collected_timestamp():
    html = render_page(snapshot()).decode()

    assert "2026-09-02 14:02:11" in html


def test_page_shows_when_the_ledger_was_last_written():
    """Beside "Collected", so `ports sync` without `install.sh` -- a stale
    server-side ledger -- is legible on the page rather than silent."""
    html = render_page(snapshot(ledger_written=datetime(2026, 9, 1, 22, 20, 0))).decode()

    assert "2026-09-01 22:20:00" in html


def test_page_says_ledger_written_is_unknown_when_the_ledger_could_not_be_read():
    """A missing or unreadable ledger file must degrade on the page, not
    render a bare `None`."""
    html = render_page(snapshot(ledger_written=None)).decode()

    assert "unknown" in html.lower()


def test_page_notes_when_docker_could_not_be_read():
    html = render_page(snapshot(docker_available=False)).decode()

    assert "docker" in html.lower()


def test_page_shows_a_collection_failure_banner():
    html = render_page(snapshot(collection_error="services.toml: boom")).decode()

    assert "services.toml: boom" in html


def test_page_shows_an_hcstatus_warning_without_calling_the_service_down():
    health = {("gte", "console"): Health(True, None, None, (), "/hcstatus unreadable")}
    html = render_page(snapshot(health=health)).decode()

    assert "UP" in html
    assert "/hcstatus unreadable" in html


def test_page_escapes_values_from_services():
    health = {("gte", "console"): Health(True, "ok", "<script>x</script>", (), None)}
    html = render_page(snapshot(health=health)).decode()

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_page_auto_refreshes():
    html = render_page(snapshot()).decode()

    assert 'http-equiv="refresh"' in html


def test_page_escapes_every_field_that_originates_outside_this_project():
    """A dozen values reach the page from places this project does not
    control: /hcstatus summaries, detail labels and values, warnings, and
    Docker-derived container/lease naming. Plant the same hostile payload in
    every one of them and confirm none of it, and no <script> tag, survives
    into the rendered page.
    """
    metrics = dict(METRICS)
    metrics["hostname"] = PAYLOAD
    metrics["uptime"] = PAYLOAD
    metrics["ipv4_address"] = PAYLOAD
    metrics["docker_container_count"] = PAYLOAD
    metrics["current_datetime"] = PAYLOAD

    health = {
        (PAYLOAD, PAYLOAD): Health(
            up=True,
            state="ok",
            summary=PAYLOAD,
            detail=(Detail(PAYLOAD, PAYLOAD),),
            warning=PAYLOAD,
        )
    }

    html = render_page(
        snapshot(
            metrics=metrics,
            leases=(Lease(PAYLOAD, PAYLOAD, PAYLOAD, PAYLOAD, 8080, date(2026, 9, 1)),),
            health=health,
            drift=(Drift(PAYLOAD, PAYLOAD),),
            collection_error=PAYLOAD,
        )
    ).decode()

    assert PAYLOAD not in html
    assert "<script>" not in html
    # The escaped form must actually show up -- otherwise a field could have
    # been silently dropped rather than escaped, and this test would still
    # pass for the wrong reason.
    assert escape(PAYLOAD) in html


def _get(handler_cls, path):
    """Drive `do_GET` directly: no real socket, no real server.

    `BaseHTTPRequestHandler.__init__` normally reads the request off a live
    socket, so the class is instantiated with `__new__` and given only the
    attributes `do_GET` and the response-writing methods it calls actually
    touch.
    """
    handler = handler_cls.__new__(handler_cls)
    handler.rfile = BytesIO(b"")
    handler.wfile = BytesIO()
    handler.client_address = ("127.0.0.1", 51234)
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"GET {path} HTTP/1.1"
    handler.command = "GET"
    handler.path = path
    handler.close_connection = True

    handler.do_GET()

    raw = handler.wfile.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ", 2)[1])
    headers = {}
    for line in lines[1:]:
        key, _, value = line.partition(b": ")
        headers[key.decode("latin-1")] = value.decode("latin-1")
    return status, headers, body


def test_handler_serves_the_page_at_root():
    handler_cls = make_handler(lambda: snapshot())

    status, headers, body = _get(handler_cls, "/")

    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert b"<html" in body.lower()


def test_handler_serves_ports_json():
    handler_cls = make_handler(lambda: snapshot())

    status, headers, body = _get(handler_cls, "/ports.json")

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    payload = json.loads(body)
    assert payload["host"] == "hpz440"


def test_handler_refuses_ports_json_before_the_first_cycle():
    """An unprobed snapshot's listener list is empty because nothing was
    looked at, not because nothing is there. Serving it as 200 reads to the
    allocator as a verified-empty host and it grants against that -- so this
    must be a 503, never a 200 with an empty `listening`.
    """
    handler_cls = make_handler(lambda: snapshot(probed=False, listeners=(), health={}))

    status, _headers, body = _get(handler_cls, "/ports.json")

    assert status == 503
    # Pin the real text: `b"not" in body.lower()` is also satisfied by the 404
    # body, `b"not found\n"`, so it proved nothing about the highest-stakes
    # gate in this module.
    assert body == b"not yet probed: no collection cycle has completed\n"


def test_handler_still_serves_the_page_before_the_first_cycle():
    """The 503 is /ports.json-only. An operator looking at the page during
    the same window must still get it, with its existing "nothing collected
    yet" notes -- not a failure of its own.
    """
    handler_cls = make_handler(lambda: snapshot(probed=False, listeners=(), health={}))

    status, _headers, body = _get(handler_cls, "/")

    assert status == 200
    assert b"<html" in body.lower()


def test_ports_json_503_reaches_the_allocator_as_live_unavailable():
    """End-to-end proof, not an assumption: drive the real `fetch_live` (the
    allocator's actual reader) against an opener that reproduces real
    `urllib` behaviour -- raising `HTTPError` for a non-2xx response, exactly
    as `urllib.request.urlopen` does against a real 503. `HTTPError` is an
    `OSError` subclass, so `fetch_live` must turn it into `LiveUnavailable`,
    which is the refusal `ports/cli.py` already knows how to fall back on.
    """
    handler_cls = make_handler(lambda: snapshot(probed=False, listeners=(), health={}))

    def opener(url, timeout):
        status, _headers, body = _get(handler_cls, "/ports.json")
        if status >= 400:
            raise urllib.error.HTTPError(url, status, "not probed", {}, BytesIO(body))
        return FakeResponse(body)

    with pytest.raises(LiveUnavailable):
        fetch_live("http://x/ports.json", opener=opener)


def test_handler_404s_an_unknown_path():
    handler_cls = make_handler(lambda: snapshot())

    status, headers, body = _get(handler_cls, "/nope")

    assert status == 404
    assert b"not found" in body


def test_handler_content_length_matches_the_body_on_every_route():
    handler_cls = make_handler(lambda: snapshot())

    for path in ("/", "/ports.json", "/nope"):
        _, headers, body = _get(handler_cls, path)
        assert int(headers["Content-Length"]) == len(body)


def test_handler_only_ever_calls_get_snapshot():
    """The handler must never collect or probe on its own -- reading the
    published snapshot is the only way it may learn anything. A route that
    called a collector directly would need something this fake does not
    provide, and a route that skipped `get_snapshot` would leave `calls`
    short of what this asserts.
    """
    calls = []

    def fake_get_snapshot():
        calls.append(1)
        return snapshot()

    handler_cls = make_handler(fake_get_snapshot)

    _get(handler_cls, "/")
    assert calls == [1]

    _get(handler_cls, "/ports.json")
    assert calls == [1, 1]

    _get(handler_cls, "/nope")
    assert calls == [1, 1]  # 404 never touches the snapshot at all


def test_handler_refuses_ports_json_when_docker_could_not_be_read():
    """The half-blind window beside the unprobed one.

    `ports_payload` derives `container` purely from `snapshot.containers`,
    which is empty both when Docker reports nothing and when Docker could not
    be asked. Served as a 200, the allocator reads a verified answer in which
    no container owns anything -- so a project already running on its wanted
    port looks unowned, loses its "already running" grandfathering, and gets a
    different port written into its `.env` because the Docker daemon was
    briefly unreachable.
    """
    handler_cls = make_handler(lambda: snapshot(docker_available=False, containers=()))

    status, headers, body = _get(handler_cls, "/ports.json")

    assert status == 503
    assert headers["Content-Type"] == "text/plain; charset=utf-8"
    assert body == b"docker could not be read: container attribution is incomplete\n"


def test_the_ports_json_refusal_says_which_window_it_is():
    """Wait, or go fix the Docker daemon -- the operator's next move differs."""
    handler_cls = make_handler(
        lambda: snapshot(probed=False, listeners=(), health={}, docker_available=False)
    )

    status, _headers, body = _get(handler_cls, "/ports.json")

    assert status == 503
    assert body == (
        b"not yet probed: no collection cycle has completed; "
        b"docker could not be read: container attribution is incomplete\n"
    )


def test_a_docker_outage_reaches_the_allocator_as_live_unavailable():
    """End-to-end, through the allocator's real reader.

    A complete-but-unattributed `LiveState` is what moves other people's
    services. `fetch_live` must never build one out of this window.
    """
    handler_cls = make_handler(lambda: snapshot(docker_available=False, containers=()))

    def opener(url, timeout):
        status, _headers, body = _get(handler_cls, "/ports.json")
        if status >= 400:
            raise urllib.error.HTTPError(url, status, "docker unavailable", {}, BytesIO(body))
        return FakeResponse(body)

    with pytest.raises(LiveUnavailable):
        fetch_live("http://x/ports.json", opener=opener)


def test_the_page_still_serves_while_docker_is_unreadable():
    """The 503 is /ports.json-only. The page reports the outage in its banner
    and keeps serving -- an operator looking at a Docker outage needs the page
    most, not least.
    """
    handler_cls = make_handler(lambda: snapshot(docker_available=False, containers=()))

    status, _headers, body = _get(handler_cls, "/")

    assert status == 200
    assert b"<html" in body.lower()
    assert b"docker could not be read" in body.lower()


def test_a_listening_non_http_lease_is_not_reported_down():
    """`services.toml` leases 1883 to ice-colder/mqtt, and the probe speaks
    only HTTP -- so that lease is `up=False` forever, on day one, on the real
    ledger. Meanwhile `find_drift` sees the listener and reports nothing
    wrong, so the first real page read "No drift: every lease matches what is
    running" directly above a red DOWN.
    """
    mqtt = Lease("ice-colder", "mqtt", "hpz440", "0.0.0.0", 1883, date(2026, 9, 1))
    html = render_page(
        snapshot(
            leases=(mqtt,),
            listeners=(Listener("0.0.0.0", 1883, None),),
            containers=(),
            health={("ice-colder", "mqtt"): Health(False, None, None, (), None)},
            drift=(),
        )
    ).decode()

    assert "DOWN" not in html
    assert "LISTENING" in html
    # And it says what that means, rather than leaving a third word unexplained.
    assert "does not" in html.lower()
    assert "http probe" in html.lower()


def test_a_lease_with_no_listener_is_still_down():
    """DOWN keeps its meaning: nothing is holding the port at all."""
    html = render_page(
        snapshot(
            listeners=(),
            containers=(),
            health={("gte", "console"): Health(False, None, None, (), None)},
        )
    ).decode()

    assert "DOWN" in html
    assert "LISTENING" not in html


def test_a_listener_on_another_address_does_not_excuse_a_dead_lease():
    """The join is `addrs_overlap`, as everywhere else in this codebase: a
    stranger's listener on an unrelated specific address says nothing about
    this lease.
    """
    lease = Lease("gte", "console", "hpz440", "127.0.0.1", 8080, date(2026, 9, 1))
    html = render_page(
        snapshot(
            leases=(lease,),
            listeners=(Listener("192.168.1.5", 8080, None),),
            containers=(),
            health={("gte", "console"): Health(False, None, None, (), None)},
        )
    ).decode()

    assert "DOWN" in html
    assert "LISTENING" not in html


def test_a_wildcard_listener_covers_a_specific_lease():
    """The overlap rule runs both directions."""
    lease = Lease("gte", "console", "hpz440", "127.0.0.1", 8080, date(2026, 9, 1))
    html = render_page(
        snapshot(
            leases=(lease,),
            listeners=(Listener("0.0.0.0", 8080, None),),
            containers=(),
            health={("gte", "console"): Health(False, None, None, (), None)},
        )
    ).decode()

    assert "LISTENING" in html
    assert "DOWN" not in html


def test_an_off_host_lease_whose_port_coincides_locally_is_not_listening():
    """`_lease_has_listener` must credit a listener only to a lease on the
    host this page serves. `snapshot.leases` is fleet-wide -- every lease in
    `services.toml`, not just this host's -- while `snapshot.listeners` is
    always local. A lease belonging to another host whose port happens to
    coincide with something listening here must not read as LISTENING for a
    service that, on this host, is not running at all.
    """
    elsewhere = Lease("elsewhere-proj", "svc", "other-host", "0.0.0.0", 9999, date(2026, 9, 1))
    html = render_page(
        snapshot(
            leases=(elsewhere,),
            listeners=(Listener("0.0.0.0", 9999, None),),
            containers=(),
            health={("elsewhere-proj", "svc"): Health(False, None, None, (), None)},
        )
    ).decode()

    assert "LISTENING" not in html
    assert "DOWN" in html


def test_the_listening_legend_is_absent_when_nothing_is_listening_only():
    """A page with no third state must not carry an explanation of one."""
    html = render_page(snapshot()).decode()

    assert "LISTENING" not in html


def test_handler_returns_500_rather_than_an_empty_reply_when_rendering_raises():
    """A `KeyError` or `ValueError` out of `render_page` used to propagate
    straight out of `do_GET` with no response written at all: the client got
    an empty reply rather than a status, on every request, while the prober
    went on publishing. That contradicts this module's promise that nothing
    takes the page down.
    """
    # A metrics dict missing `hostname` is the real shape of this failure.
    handler_cls = make_handler(lambda: snapshot(metrics={}))

    status, headers, body = _get(handler_cls, "/")

    assert status == 500
    assert headers["Content-Type"] == "text/plain; charset=utf-8"
    assert int(headers["Content-Length"]) == len(body)
    assert body == b"internal error\n"


def test_handler_returns_500_when_the_snapshot_source_itself_raises():
    """The same guard covers /ports.json, and a `get_snapshot` that fails."""

    def boom():
        raise ValueError("the holder is wedged")

    handler_cls = make_handler(boom)

    status, _headers, body = _get(handler_cls, "/ports.json")

    assert status == 500
    assert body == b"internal error\n"
