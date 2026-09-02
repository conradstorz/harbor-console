import json
from datetime import date, datetime
from html import escape
from io import BytesIO

from harbor_console.docker import Container
from harbor_console.listening import Listener
from harbor_console.ports.ledger import Lease
from harbor_console.ports.live import fetch_live
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


def test_page_shows_the_collected_timestamp():
    html = render_page(snapshot()).decode()

    assert "2026-09-02 14:02:11" in html


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
