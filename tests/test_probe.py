import http.client
import json
import urllib.error

import pytest

import harbor_console.probe as probe_module
from harbor_console.probe import Detail, probe

HCSTATUS = {
    "state": "ok",
    "summary": "3 queued",
    "detail": [{"label": "queue", "value": "3"}],
}


class FakeResponse:
    def __init__(self, body=b""):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self._body


def opener_for(routes):
    """routes: url suffix -> body bytes, or an exception instance to raise."""

    def opener(url, timeout):
        for suffix, outcome in routes.items():
            if url.endswith(suffix):
                if isinstance(outcome, Exception):
                    raise outcome
                return FakeResponse(outcome)
        raise urllib.error.URLError("no route")

    return opener


def http_error(code):
    return urllib.error.HTTPError("http://x/", code, "err", {}, None)


def test_any_response_means_up():
    health = probe("h", 1, opener=opener_for({"/": b"", "/hcstatus": http_error(404)}))

    assert health.up is True


def test_a_404_on_the_root_still_means_up():
    routes = {"/hcstatus": http_error(404), "/": http_error(404)}

    assert probe("h", 1, opener=opener_for(routes)).up is True


def test_connection_refused_means_down():
    routes = {
        "/": urllib.error.URLError("refused"),
        "/hcstatus": urllib.error.URLError("x"),
    }

    health = probe("h", 1, opener=opener_for(routes))

    assert health.up is False
    assert health.detail == ()


def test_a_timeout_means_down():
    routes = {"/": TimeoutError(), "/hcstatus": TimeoutError()}

    assert probe("h", 1, opener=opener_for(routes)).up is False


def test_hcstatus_detail_is_parsed():
    routes = {"/hcstatus": json.dumps(HCSTATUS).encode(), "/": b""}

    health = probe("h", 1, opener=opener_for(routes))

    assert health.state == "ok"
    assert health.summary == "3 queued"
    assert health.detail == (Detail("queue", "3"),)
    assert health.warning is None


def test_a_missing_hcstatus_is_not_a_warning():
    routes = {"/hcstatus": http_error(404), "/": b""}

    health = probe("h", 1, opener=opener_for(routes))

    assert health.up is True
    assert health.warning is None
    assert health.detail == ()


def test_malformed_hcstatus_json_warns_but_stays_up():
    routes = {"/hcstatus": b"not json", "/": b""}

    health = probe("h", 1, opener=opener_for(routes))

    assert health.up is True
    assert health.warning is not None
    assert health.detail == ()


def test_wrong_shaped_hcstatus_warns_but_stays_up():
    routes = {"/hcstatus": json.dumps({"state": 5}).encode(), "/": b""}

    health = probe("h", 1, opener=opener_for(routes))

    assert health.up is True
    assert health.warning is not None


def test_a_hung_hcstatus_warns_but_stays_up():
    routes = {"/hcstatus": TimeoutError(), "/": b""}

    health = probe("h", 1, opener=opener_for(routes))

    assert health.up is True
    assert health.warning is not None


def test_detail_rows_that_are_not_label_value_are_dropped():
    body = json.dumps(
        {"state": "ok", "summary": "x", "detail": ["nope", {"label": "a"}]}
    )
    routes = {"/hcstatus": body.encode(), "/": b""}

    health = probe("h", 1, opener=opener_for(routes))

    assert health.detail == ()
    assert health.warning is not None


def test_a_malformed_status_line_on_root_means_down():
    """http.client.HTTPException is not an OSError -- must still mean down."""
    routes = {
        "/": http.client.BadStatusLine("garbage"),
        "/hcstatus": http_error(404),
    }

    health = probe("h", 1, opener=opener_for(routes))

    assert health.up is False
    assert health.detail == ()


class ReadRaisesResponse:
    """A response whose body read fails after a successful connect."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        raise http.client.IncompleteRead(b"partial")


def test_an_incomplete_hcstatus_body_warns_but_stays_up():
    """http.client.IncompleteRead surfaces from response.read(), not the
    opener call itself -- must still be caught at the fetch boundary."""

    def opener(url, timeout):
        if url.endswith("/hcstatus"):
            return ReadRaisesResponse()
        return FakeResponse(b"")

    health = probe("h", 1, opener=opener)

    assert health.up is True
    assert health.warning is not None


def test_hcstatus_500_warns_but_stays_up():
    routes = {"/hcstatus": http_error(500), "/": b""}

    health = probe("h", 1, opener=opener_for(routes))

    assert health.up is True
    assert health.warning is not None


def test_hcstatus_404_still_produces_no_warning():
    routes = {"/hcstatus": http_error(404), "/": b""}

    health = probe("h", 1, opener=opener_for(routes))

    assert health.up is True
    assert health.warning is None


def test_a_bug_in_hcstatus_parsing_is_not_swallowed(monkeypatch):
    """The fetch boundary must not widen into this module's own parsing and
    validation logic: a genuine bug there has to propagate, not become a
    silent warning."""

    def broken_json_loads(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(probe_module.json, "loads", broken_json_loads)
    routes = {"/hcstatus": b"{}", "/": b""}

    with pytest.raises(RuntimeError):
        probe("h", 1, opener=opener_for(routes))
