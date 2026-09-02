import json
from datetime import date, datetime

from harbor_console.docker import Container
from harbor_console.listening import Listener
from harbor_console.ports.ledger import Lease
from harbor_console.ports.live import fetch_live
from harbor_console.probe import Detail, Health
from harbor_console.snapshot import Drift, Snapshot
from harbor_console.web import ports_payload, render_page

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
        ledger_error=None,
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


def test_page_shows_the_collected_timestamp():
    html = render_page(snapshot()).decode()

    assert "2026-09-02 14:02:11" in html


def test_page_notes_when_docker_could_not_be_read():
    html = render_page(snapshot(docker_available=False)).decode()

    assert "docker" in html.lower()


def test_page_shows_a_ledger_error_banner():
    html = render_page(snapshot(ledger_error="services.toml: boom")).decode()

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
