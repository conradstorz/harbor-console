import json
import urllib.error

from harbor_console.traefik import (
    TRAEFIK_UNAVAILABLE,
    Router,
    router_name,
    traefik_routers,
)


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self._body


def opener_for(routers, raises=None):
    def opener(url, timeout=None):
        if raises is not None:
            raise raises
        assert url == "http://127.0.0.1:8081/api/http/routers"
        return FakeResponse(json.dumps(routers).encode())

    return opener


def test_parses_host_service_and_status():
    routers = [
        {
            "name": "parksmart@docker",
            "rule": "Host(`parksmart.hpz440.ohr3023.org`)",
            "service": "parksmart",
            "status": "enabled",
        }
    ]

    assert traefik_routers(opener=opener_for(routers)) == (
        Router("parksmart@docker", "parksmart.hpz440.ohr3023.org", "parksmart", True, None),
    )


def test_a_disabled_router_carries_its_error():
    routers = [
        {
            "name": "bad@docker",
            "rule": "Host(`bad.hpz440.ohr3023.org`)",
            "service": "bad",
            "status": "disabled",
            "error": ["service \"bad@docker\" does not exist"],
        }
    ]

    router = traefik_routers(opener=opener_for(routers))[0]

    assert router.enabled is False
    assert router.error == 'service "bad@docker" does not exist'


def test_a_rule_without_host_yields_none():
    routers = [{"name": "p@file", "rule": "PathPrefix(`/x`)", "service": "p", "status": "enabled"}]

    assert traefik_routers(opener=opener_for(routers))[0].host is None


def test_routers_sort_by_name():
    routers = [
        {"name": "z@docker", "rule": "Host(`z`)", "service": "z", "status": "enabled"},
        {"name": "a@docker", "rule": "Host(`a`)", "service": "a", "status": "enabled"},
    ]

    assert [r.name for r in traefik_routers(opener=opener_for(routers))] == ["a@docker", "z@docker"]


def test_an_entry_missing_its_name_is_skipped():
    routers = [{"rule": "Host(`x`)"}, {"name": "ok@docker", "rule": "Host(`ok`)", "service": "ok", "status": "enabled"}]

    assert [r.name for r in traefik_routers(opener=opener_for(routers))] == ["ok@docker"]


def test_unreachable_api_is_unavailable():
    result = traefik_routers(opener=opener_for([], raises=urllib.error.URLError("refused")))

    assert result is TRAEFIK_UNAVAILABLE


def test_http_error_is_unavailable():
    err = urllib.error.HTTPError("u", 500, "boom", {}, None)

    assert traefik_routers(opener=opener_for([], raises=err)) is TRAEFIK_UNAVAILABLE


def test_non_json_is_unavailable():
    def opener(url, timeout=None):
        return FakeResponse(b"<html>")

    assert traefik_routers(opener=opener) is TRAEFIK_UNAVAILABLE


def test_empty_router_list_is_not_unavailable():
    result = traefik_routers(opener=opener_for([]))

    assert result == ()
    assert result is not TRAEFIK_UNAVAILABLE


def test_router_name_appends_the_docker_provider():
    assert router_name("parksmart") == "parksmart@docker"
