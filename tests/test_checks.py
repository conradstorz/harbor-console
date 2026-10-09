from datetime import datetime, timedelta, timezone

from harbor_console.checks import (
    CHECK_PROBER_FRESH,
    STALE_AFTER_SECONDS,
    STATE_FAILED,
    STATE_OK,
    STATE_UNKNOWN,
    Check,
    freshness_check,
    is_stale,
    platform_broken,
)

# Timezone-aware so the certificate day counts in Task 3 do not depend on
# the workstation's local timezone.
NOW = datetime(2026, 10, 9, 17, 21, 46, tzinfo=timezone.utc)


def test_platform_broken_when_any_check_failed():
    checks = (
        Check("docker", STATE_OK, "answered"),
        Check("own-route", STATE_FAILED, "504"),
    )

    assert platform_broken(checks) is True


def test_platform_not_broken_by_unknown_checks():
    checks = (
        Check("docker", STATE_OK, "answered"),
        Check("certificate", STATE_UNKNOWN, "no tailnet address"),
    )

    assert platform_broken(checks) is False


def test_platform_not_broken_with_no_checks():
    assert platform_broken(()) is False


def test_is_stale_at_the_threshold():
    written = NOW - timedelta(seconds=STALE_AFTER_SECONDS)

    assert is_stale(written, NOW) is False
    assert is_stale(written - timedelta(seconds=1), NOW) is True


def test_freshness_check_is_unknown_without_a_verdict():
    check = freshness_check(None, NOW)

    assert check.name == CHECK_PROBER_FRESH
    assert check.state == STATE_UNKNOWN


def test_freshness_check_fails_when_stale():
    check = freshness_check(NOW - timedelta(seconds=200), NOW)

    assert check.state == STATE_FAILED
    assert "200 s ago" in check.reason


def test_freshness_check_passes_when_recent():
    check = freshness_check(NOW - timedelta(seconds=12), NOW)

    assert check.state == STATE_OK
    assert "12 s ago" in check.reason


from harbor_console.certificate import Certificate, CertificateUnavailable
from harbor_console.checks import (
    CHECK_CERTIFICATE,
    CHECK_DOCKER,
    CHECK_DOCKER_PROVIDER,
    CHECK_EDGE_LISTENING,
    CHECK_OWN_ROUTE,
    CHECK_TRAEFIK_API,
    run_checks,
)
from harbor_console.docker import Container
from harbor_console.listening import Listener
from harbor_console.probe import Health
from harbor_console.traefik import Router

TAILNET = "100.69.239.123"
UP = Health(True, None, None, (), None)
DOWN = Health(False, None, None, (), None)
ROUTED = Container(
    "gte-admin-1",
    (),
    {"traefik.enable": "true", "traefik.http.routers.gte.rule": "Host(`gte.hpz440.ohr3023.org`)"},
    frozenset({"harbor"}),
)
INTERNAL = Container("gte-db-1", (), {"harbor.kind": "internal"}, frozenset())
DOCKER_ROUTER = Router("gte@docker", "gte.hpz440.ohr3023.org", "gte", True, None)
FILE_ROUTER = Router("harbor@file", "harbor.hpz440.ohr3023.org", "harbor", True, None)
GOOD_CERT = Certificate(
    ("hpz440.ohr3023.org", "*.hpz440.ohr3023.org"),
    datetime(2026, 12, 18, 12, 5, 54, tzinfo=timezone.utc),
)
EDGE = (Listener(TAILNET, 80, 1), Listener(TAILNET, 443, 1))


def checks(**overrides):
    kwargs = dict(
        now=NOW,
        docker_available=True,
        traefik_available=True,
        containers=(ROUTED, INTERNAL),
        routers=(DOCKER_ROUTER, FILE_ROUTER),
        own_route=UP,
        certificate=GOOD_CERT,
        listeners=EDGE,
        listeners_available=True,
        tailnet_address=TAILNET,
    )
    kwargs.update(overrides)
    return {check.name: check for check in run_checks(**kwargs)}


def test_run_checks_reports_six_checks_in_a_fixed_order():
    names = [check.name for check in run_checks(
        now=NOW, docker_available=True, traefik_available=True, containers=(),
        routers=(), own_route=None, certificate=None, listeners=(),
        listeners_available=True, tailnet_address=None,
    )]

    assert names == [
        CHECK_DOCKER, CHECK_TRAEFIK_API, CHECK_DOCKER_PROVIDER,
        CHECK_OWN_ROUTE, CHECK_CERTIFICATE, CHECK_EDGE_LISTENING,
    ]


def test_a_healthy_host_passes_every_check():
    assert {c.state for c in checks().values()} == {STATE_OK}


def test_docker_fails_when_unavailable():
    assert checks(docker_available=False)[CHECK_DOCKER].state == STATE_FAILED


def test_traefik_api_fails_when_unavailable():
    assert checks(traefik_available=False)[CHECK_TRAEFIK_API].state == STATE_FAILED


def test_docker_provider_fails_when_routes_are_declared_but_traefik_has_no_docker_router():
    # The 2026-10-08 outage: Docker 29 refused Traefik v3.3's API version,
    # Traefik kept running with only its file-provider routes.
    check = checks(routers=(FILE_ROUTER,))[CHECK_DOCKER_PROVIDER]

    assert check.state == STATE_FAILED
    assert "1 container" in check.reason
    assert "@docker" in check.reason


def test_docker_provider_is_unknown_without_docker():
    assert checks(docker_available=False)[CHECK_DOCKER_PROVIDER].state == STATE_UNKNOWN


def test_docker_provider_is_unknown_without_traefik():
    assert checks(traefik_available=False)[CHECK_DOCKER_PROVIDER].state == STATE_UNKNOWN


def test_docker_provider_is_unknown_when_no_container_declares_a_route():
    check = checks(containers=(INTERNAL,), routers=(FILE_ROUTER,))[CHECK_DOCKER_PROVIDER]

    assert check.state == STATE_UNKNOWN


def test_own_route_fails_when_the_proxy_did_not_answer():
    check = checks(own_route=DOWN)[CHECK_OWN_ROUTE]

    assert check.state == STATE_FAILED
    assert "https://harbor.hpz440.ohr3023.org/" in check.reason


def test_own_route_is_unknown_without_a_tailnet_address():
    assert checks(own_route=None, tailnet_address=None)[CHECK_OWN_ROUTE].state == STATE_UNKNOWN


def test_certificate_passes_with_the_wildcard_and_time_left():
    check = checks()[CHECK_CERTIFICATE]

    assert check.state == STATE_OK
    assert "*.hpz440.ohr3023.org" in check.reason
    assert "69 days" in check.reason


def test_certificate_fails_when_the_handshake_failed():
    check = checks(certificate=CertificateUnavailable("SSLError: handshake failure"))[CHECK_CERTIFICATE]

    assert check.state == STATE_FAILED
    assert "handshake failure" in check.reason


def test_certificate_fails_without_the_wildcard_name():
    cert = Certificate(("hpz440.ohr3023.org",), GOOD_CERT.not_after)

    check = checks(certificate=cert)[CHECK_CERTIFICATE]

    assert check.state == STATE_FAILED
    assert "*.hpz440.ohr3023.org" in check.reason


def test_certificate_fails_inside_the_renewal_margin():
    cert = Certificate(GOOD_CERT.names, datetime(2026, 10, 20, tzinfo=timezone.utc))

    check = checks(certificate=cert)[CHECK_CERTIFICATE]

    assert check.state == STATE_FAILED
    assert "10 days" in check.reason


def test_certificate_is_unknown_when_not_attempted():
    assert checks(certificate=None)[CHECK_CERTIFICATE].state == STATE_UNKNOWN


def test_edge_listening_fails_when_443_is_not_bound():
    check = checks(listeners=(Listener(TAILNET, 80, 1),))[CHECK_EDGE_LISTENING]

    assert check.state == STATE_FAILED
    assert "443" in check.reason


def test_edge_listening_fails_on_a_wildcard_bind():
    # 0.0.0.0 publishes the edge to the LAN, which the bind is supposed to
    # prevent (ADR 7, ADR 15); nothing else reports it because the socket
    # is accounted to the Traefik container.
    check = checks(listeners=(Listener("0.0.0.0", 80, 1), Listener("0.0.0.0", 443, 1)))[CHECK_EDGE_LISTENING]

    assert check.state == STATE_FAILED
    assert "0.0.0.0" in check.reason
    assert "80 and 443" in check.reason


def test_edge_listening_fails_when_one_port_is_wildcard_and_the_other_is_right():
    check = checks(listeners=(Listener(TAILNET, 80, 1), Listener("0.0.0.0", 443, 1)))[CHECK_EDGE_LISTENING]

    assert check.state == STATE_FAILED
    assert "0.0.0.0:443" in check.reason


def test_edge_listening_ignores_udp_on_the_same_ports():
    udp = (Listener(TAILNET, 80, 1, "udp"), Listener(TAILNET, 443, 1, "udp"))

    assert checks(listeners=udp)[CHECK_EDGE_LISTENING].state == STATE_FAILED


def test_edge_listening_is_unknown_without_the_socket_table():
    assert checks(listeners_available=False)[CHECK_EDGE_LISTENING].state == STATE_UNKNOWN


def test_edge_listening_is_unknown_without_a_tailnet_address():
    assert checks(tailnet_address=None)[CHECK_EDGE_LISTENING].state == STATE_UNKNOWN
