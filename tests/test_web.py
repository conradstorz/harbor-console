from datetime import datetime
from html import escape
from io import BytesIO

from harbor_console import web
from harbor_console.directory import (
    KIND_HTTP,
    KIND_INTERNAL,
    KIND_TCP,
    UNDECLARED_CONTAINER,
    Finding,
    Row,
)
from harbor_console.probe import Detail, Health
from harbor_console.snapshot import Snapshot

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
PARKSMART = Row("parksmart", KIND_HTTP, "https://parksmart.hpz440.ohr3023.org/", "parksmart-parksmart-1", "parking", "UP")
MQTT = Row("ice-colder-mqtt", KIND_TCP, "0.0.0.0:1883", "ice-colder-mqtt", "", "LISTENING")
DB = Row("gte-db-1", KIND_INTERNAL, "", "gte-db-1", "postgres", "INTERNAL")


def snapshot(**overrides):
    fields = dict(collected=NOW, metrics=METRICS, rows=(PARKSMART, MQTT, DB), probed=True, tailnet_address="100.69.239.123")
    fields.update(overrides)
    return Snapshot(**fields)


def test_page_shows_host_metrics():
    page = web.render_page(snapshot()).decode()

    assert "<h1>hpz440</h1>" in page
    assert "1d 00:00:00" in page
    assert "100.69.239.123" in page


def test_http_row_links_its_route():
    page = web.render_page(snapshot()).decode()

    assert '<a href="https://parksmart.hpz440.ohr3023.org/">https://parksmart.hpz440.ohr3023.org/</a>' in page
    assert "parking" in page
    assert "parksmart-parksmart-1" in page


def test_tcp_row_prints_its_port_unlinked():
    page = web.render_page(snapshot()).decode()

    assert "0.0.0.0:1883" in page
    assert 'href="0.0.0.0:1883"' not in page


def test_down_is_emphasised():
    down = Row("x", KIND_HTTP, "https://x/", "x-1", "", "DOWN")

    page = web.render_page(snapshot(rows=(down,))).decode()

    assert '<span class="down">DOWN</span>' in page


def test_route_error_is_emphasised():
    bad = Row("x", KIND_HTTP, "", "x-1", "", "ROUTE ERROR")

    page = web.render_page(snapshot(rows=(bad,))).decode()

    assert '<span class="down">ROUTE ERROR</span>' in page


def test_a_tcp_row_named_like_a_router_gets_no_health_detail():
    """`health` is keyed by router name; only HTTP rows may look in it. A
    tcp or internal container that happens to share a router's name must not
    inherit another service's detail rows."""
    tcp = Row("parksmart", KIND_TCP, "0.0.0.0:1883", "parksmart-mqtt-1", "", "LISTENING")
    health = {"parksmart": Health(True, "ok", "3 queued", (Detail("queue", "3"),), "a warning")}

    page = web.render_page(snapshot(rows=(tcp,), health=health)).decode()

    assert "3 queued" not in page
    assert "queue: 3" not in page
    assert "a warning" not in page


def test_no_rows_says_so():
    page = web.render_page(snapshot(rows=())).decode()

    assert "No services are declared." in page


def test_page_shows_findings():
    finding = Finding(UNDECLARED_CONTAINER, "container 'mystery' carries no label")

    page = web.render_page(snapshot(findings=(finding,))).decode()

    assert "undeclared-container" in page
    assert "container &#x27;mystery&#x27; carries no label" in page


def test_page_says_so_when_there_are_no_findings():
    page = web.render_page(snapshot()).decode()

    assert "No findings: every container is declared and every route is live." in page


def test_page_does_not_call_an_unprobed_host_clean():
    page = web.render_page(snapshot(probed=False, rows=())).decode()

    assert "No findings" not in page
    assert "No services are declared" not in page
    assert page.lower().count("nothing has been collected yet") == 2


def test_page_notes_when_docker_could_not_be_read():
    page = web.render_page(snapshot(docker_available=False)).decode()

    assert "Docker could not be read" in page


def test_page_notes_when_traefik_could_not_be_read():
    page = web.render_page(snapshot(traefik_available=False)).decode()

    assert "Traefik could not be read" in page


def test_page_shows_a_collection_failure_banner():
    page = web.render_page(snapshot(collection_error="psutil exploded")).decode()

    assert "The last collection cycle failed: psutil exploded. Showing the last good page." in page


def test_banner_does_not_claim_a_last_good_page_before_the_first_cycle():
    page = web.render_page(snapshot(probed=False, collection_error="boom")).decode()

    assert "Showing the last good page" not in page
    assert "The last collection cycle failed: boom." in page


def test_page_escapes_every_field_that_originates_outside_this_project():
    evil = Row("<b>n</b>", KIND_HTTP, "https://x/?a=<s>", "<i>c</i>", "<u>d</u>", "UP")
    finding = Finding("<k>", "<d>")
    metrics = dict(METRICS, hostname="<h>")

    page = web.render_page(snapshot(rows=(evil,), findings=(finding,), metrics=metrics)).decode()

    for raw in ("<b>n</b>", "<s>", "<i>c</i>", "<u>d</u>", "<k>", "<d>", "<h>"):
        assert raw not in page
        assert escape(raw) in page


def test_page_auto_refreshes():
    assert 'http-equiv="refresh" content="30"' in web.render_page(snapshot()).decode()


def test_page_shows_the_collected_timestamp():
    assert "Collected 2026-09-02 14:02:11" in web.render_page(snapshot()).decode()


def test_handler_serves_the_page_at_root():
    status, headers, body = _get(web.make_handler(snapshot), "/")

    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"<h1>hpz440</h1>" in body


def test_handler_404s_ports_json():
    status, _headers, _body = _get(web.make_handler(snapshot), "/ports.json")

    assert status == 404


def test_handler_answers_head_with_headers_and_no_body():
    get_status, get_headers, get_body = _get(web.make_handler(snapshot), "/")
    status, headers, body = _get(web.make_handler(snapshot), "/", method="HEAD")

    assert status == 200
    assert headers["Content-Type"] == get_headers["Content-Type"]
    assert int(headers["Content-Length"]) == len(get_body)
    assert body == b""


def test_handler_404s_an_unknown_path():
    assert _get(web.make_handler(snapshot), "/nope")[0] == 404


def test_handler_content_length_matches_the_body():
    _status, headers, body = _get(web.make_handler(snapshot), "/")

    assert int(headers["Content-Length"]) == len(body)


def test_handler_answers_500_rather_than_nothing_when_rendering_raises():
    status, _headers, body = _get(web.make_handler(lambda: Snapshot(NOW, {})), "/")

    assert status == 500
    assert body == b"internal error\n"


def _get(handler_cls, path, method="GET"):
    """Drive `do_GET` (or `do_HEAD`) directly: no real socket, no real server.

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
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.command = method
    handler.path = path
    handler.close_connection = True

    getattr(handler, f"do_{method}")()

    raw = handler.wfile.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ", 2)[1])
    headers = {}
    for line in lines[1:]:
        key, _, value = line.partition(b": ")
        headers[key.decode("latin-1")] = value.decode("latin-1")
    return status, headers, body
