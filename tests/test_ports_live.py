import json

import pytest

from harbor_console.ports.live import (
    LiveState,
    LiveUnavailable,
    Listener,
    fetch_live,
    probe_live,
)

PAYLOAD = {
    "host": "hpz440",
    "collected": "2026-09-01T14:02:11Z",
    "listening": [
        {"addr": "0.0.0.0", "port": 8080, "container": "acme"},
        {"addr": "127.0.0.1", "port": 5432, "container": "shared-postgres"},
        {"addr": "100.69.239.123", "port": 49152, "container": "delta-rippers-dev"},
        {"addr": "0.0.0.0", "port": 22, "container": None},
    ],
}


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self) -> bytes:
        return self._body


def test_fetch_parses_listeners_including_non_docker(monkeypatch):
    state = fetch_live(
        "http://hpz440:8090/ports.json",
        opener=lambda _url, timeout: FakeResponse(json.dumps(PAYLOAD).encode()),
    )

    assert state.host == "hpz440"
    assert state.complete is True
    assert len(state.listeners) == 4
    assert state.listeners[3].container is None


def test_is_listening_treats_any_addr_as_covering_specifics():
    state = fetch_live(
        "http://x/ports.json",
        opener=lambda _url, timeout: FakeResponse(json.dumps(PAYLOAD).encode()),
    )

    assert state.is_listening("100.69.239.123", 8080) is True
    assert state.is_listening("0.0.0.0", 49152) is True
    assert state.is_listening("127.0.0.1", 8080) is True
    assert state.is_listening("0.0.0.0", 9999) is False


def test_fetch_raises_live_unavailable_on_transport_error():
    def boom(_url, timeout):
        raise OSError("connection refused")

    with pytest.raises(LiveUnavailable, match="connection refused"):
        fetch_live("http://hpz440:8090/ports.json", opener=boom)


def test_fetch_raises_live_unavailable_on_bad_json():
    with pytest.raises(LiveUnavailable):
        fetch_live("http://x/ports.json", opener=lambda _u, timeout: FakeResponse(b"not json"))


def test_fetch_raises_live_unavailable_when_port_is_float():
    payload = {
        "host": "hpz440",
        "listening": [
            {"addr": "0.0.0.0", "port": 8080.7, "container": "acme"},
        ],
    }
    with pytest.raises(LiveUnavailable, match="port must be an integer"):
        fetch_live(
            "http://x/ports.json",
            opener=lambda _url, timeout: FakeResponse(json.dumps(payload).encode()),
        )


def test_fetch_raises_live_unavailable_when_port_is_string():
    payload = {
        "host": "hpz440",
        "listening": [
            {"addr": "0.0.0.0", "port": "8080", "container": "acme"},
        ],
    }
    with pytest.raises(LiveUnavailable, match="port must be an integer"):
        fetch_live(
            "http://x/ports.json",
            opener=lambda _url, timeout: FakeResponse(json.dumps(payload).encode()),
        )


def test_container_on_with_an_address_matches_the_overlapping_listener():
    state = LiveState(
        host="hpz440",
        listeners=(
            Listener(addr="10.0.0.5", port=49152, container="delta-dev"),
            Listener(addr="127.0.0.1", port=49152, container="stranger"),
        ),
        complete=True,
    )

    assert state.container_on(49152, "10.0.0.5") == "delta-dev"
    assert state.container_on(49152, "127.0.0.1") == "stranger"


def test_container_on_with_an_address_returns_none_when_nothing_overlaps():
    state = LiveState(
        host="hpz440",
        listeners=(Listener(addr="127.0.0.1", port=49152, container="stranger"),),
        complete=True,
    )

    assert state.container_on(49152, "10.0.0.5") is None


def test_probe_marks_state_incomplete_and_reports_open_ports():
    class FakeSocket:
        def close(self):
            pass

    def connect(address, timeout):
        if address[1] == 8080:
            return FakeSocket()
        raise OSError("refused")

    state = probe_live("hpz440", [8080, 8090], connect=connect)

    assert state.complete is False
    assert state.is_listening("0.0.0.0", 8080) is True
    assert state.is_listening("0.0.0.0", 8090) is False
