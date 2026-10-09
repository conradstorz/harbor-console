"""The platform checks: is the host's own machinery working?

Pure policy, like `directory.py`: evidence in, named checks out, so every
rule is testable with plain values. Nothing here collects or renders.

Platform only. A single declared service being down is a row-level DOWN in
the directory, never a check here: a banner that is red half the year
because of one dev container stops meaning anything, and the next edge
outage hides behind it. The checks cover the three outages that have
already happened -- Traefik's Docker provider dying under a Docker upgrade,
Traefik's default route landing on the wrong network (ADR 17), a reboot
moving Traefik off the address the firewall rule named (ADR 19) -- and the
one that has not yet: a certificate renewal failing silently.

`unknown` never trips the banner. A check whose evidence is missing says so
and stays out of the verdict, the same rule the findings follow (ADR 18):
absence of evidence is never a finding.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from harbor_console.certificate import Certificate, CertificateUnavailable
from harbor_console.directory import KIND_HTTP, declared_kind, route_url
from harbor_console.docker import Container
from harbor_console.listening import ANY_ADDR, PROTO_TCP, Listener
from harbor_console.probe import Health
from harbor_console.traefik import Router

STATE_OK = "ok"
STATE_FAILED = "failed"
STATE_UNKNOWN = "unknown"

CHECK_DOCKER = "docker"
CHECK_TRAEFIK_API = "traefik-api"
CHECK_DOCKER_PROVIDER = "docker-provider"
CHECK_OWN_ROUTE = "own-route"
CHECK_CERTIFICATE = "certificate"
CHECK_EDGE_LISTENING = "edge-listening"
CHECK_PROBER_FRESH = "prober-fresh"

#: The page's own route through Traefik, from `deploy/traefik/dynamic/
#: harbor.yml.in`. Probing it is the one thing the page does not do today,
#: and it is the path both ADR 17 and ADR 19 broke.
OWN_ROUTE_HOST = "harbor.hpz440.ohr3023.org"

#: The name the wildcard certificate must carry (ADR 15).
WILDCARD_NAME = "*.hpz440.ohr3023.org"

#: Well inside Let's Encrypt's 30-day renewal window, so a renewal that has
#: silently failed shows two weeks before it matters. A constant, not
#: configuration (ADR 3).
CERTIFICATE_MIN_DAYS = 14

#: Three missed 30 s probe cycles. One slow cycle never flashes the banner.
STALE_AFTER_SECONDS = 90.0

#: The two ports the edge publishes on the tailnet address (ADR 15).
EDGE_PORTS = (80, 443)


@dataclass(frozen=True)
class Check:
    """One named judgement: what was checked, how it went, and why."""

    name: str
    state: str
    reason: str


def platform_broken(checks: Sequence[Check]) -> bool:
    """True when any check failed. Unknown never counts."""
    return any(check.state == STATE_FAILED for check in checks)


def is_stale(written: datetime, now: datetime) -> bool:
    """True when a verdict is older than the staleness threshold."""
    return (now - written).total_seconds() > STALE_AFTER_SECONDS


def freshness_check(written: datetime | None, now: datetime) -> Check:
    """The prober-fresh check, judged by whoever reads the verdict.

    A writer cannot report its own silence, so this one is computed on the
    reading side: the page from its snapshot's `collected`, the console and
    the check command from the file's `written`. `None` means nothing has
    been written yet, which is unknown, not failed.
    """
    if written is None:
        return Check(CHECK_PROBER_FRESH, STATE_UNKNOWN, "no verdict has been written yet")
    age = int((now - written).total_seconds())
    if is_stale(written, now):
        return Check(
            CHECK_PROBER_FRESH,
            STATE_FAILED,
            f"status page has not reported since {written:%H:%M:%S}, {age} s ago",
        )
    return Check(CHECK_PROBER_FRESH, STATE_OK, f"reported {age} s ago")


def run_checks(
    *,
    now: datetime,
    docker_available: bool,
    traefik_available: bool,
    containers: Sequence[Container],
    routers: Sequence[Router],
    own_route: Health | None,
    certificate: Certificate | CertificateUnavailable | None,
    listeners: Sequence[Listener],
    listeners_available: bool,
    tailnet_address: str | None,
) -> tuple[Check, ...]:
    """Judge the platform from one cycle's evidence. Fixed order, six checks.

    Plain values rather than a `Snapshot`, so this module never imports the
    one that carries its result. `own_route` and `certificate` are `None`
    when they were not attempted (no tailnet address), which is unknown;
    a probe that was attempted and failed is failed.
    """
    return (
        _docker(docker_available),
        _traefik_api(traefik_available),
        _docker_provider(docker_available, traefik_available, containers, routers),
        _own_route(own_route),
        _certificate(certificate, now, tailnet_address),
        _edge_listening(listeners, listeners_available, tailnet_address),
    )


def _docker(available: bool) -> Check:
    if not available:
        return Check(CHECK_DOCKER, STATE_FAILED, "Docker could not be read")
    return Check(CHECK_DOCKER, STATE_OK, "Docker answered")


def _traefik_api(available: bool) -> Check:
    if not available:
        return Check(
            CHECK_TRAEFIK_API, STATE_FAILED, "Traefik's API at 127.0.0.1:8081 could not be read"
        )
    return Check(CHECK_TRAEFIK_API, STATE_OK, "Traefik's API answered")


def _docker_provider(
    docker_available: bool,
    traefik_available: bool,
    containers: Sequence[Container],
    routers: Sequence[Router],
) -> Check:
    if not docker_available or not traefik_available:
        return Check(CHECK_DOCKER_PROVIDER, STATE_UNKNOWN, "needs both Docker and Traefik")
    declared = [c for c in containers if declared_kind(c) == KIND_HTTP]
    if not declared:
        return Check(CHECK_DOCKER_PROVIDER, STATE_UNKNOWN, "no container declares a route")
    from_docker = [r for r in routers if r.name.endswith("@docker")]
    noun = "container" if len(declared) == 1 else "containers"
    verb = "declares" if len(declared) == 1 else "declare"
    if not from_docker:
        return Check(
            CHECK_DOCKER_PROVIDER,
            STATE_FAILED,
            f"{len(declared)} {noun} {verb} a route but Traefik reports no @docker router",
        )
    return Check(
        CHECK_DOCKER_PROVIDER,
        STATE_OK,
        f"{len(from_docker)} @docker routers from {len(declared)} {noun}",
    )


def _own_route(health: Health | None) -> Check:
    url = route_url(OWN_ROUTE_HOST)
    if health is None:
        return Check(CHECK_OWN_ROUTE, STATE_UNKNOWN, "not probed: no tailnet address")
    if not health.up:
        return Check(CHECK_OWN_ROUTE, STATE_FAILED, f"{url} did not answer through the proxy")
    return Check(CHECK_OWN_ROUTE, STATE_OK, f"{url} answers through the proxy")


def _certificate(
    certificate: Certificate | CertificateUnavailable | None,
    now: datetime,
    tailnet_address: str | None,
) -> Check:
    if certificate is None:
        return Check(CHECK_CERTIFICATE, STATE_UNKNOWN, "not checked: no tailnet address")
    if isinstance(certificate, CertificateUnavailable):
        return Check(
            CHECK_CERTIFICATE,
            STATE_FAILED,
            f"TLS to {tailnet_address}:443 failed: {certificate.reason}",
        )
    if WILDCARD_NAME not in certificate.names:
        covered = ", ".join(certificate.names) or "nothing"
        return Check(
            CHECK_CERTIFICATE,
            STATE_FAILED,
            f"the served certificate covers {covered}, not {WILDCARD_NAME}",
        )
    now_utc = now.astimezone(timezone.utc)
    days = (certificate.not_after - now_utc).days
    if days < CERTIFICATE_MIN_DAYS:
        return Check(CHECK_CERTIFICATE, STATE_FAILED, f"the certificate expires in {days} days")
    return Check(CHECK_CERTIFICATE, STATE_OK, f"covers {WILDCARD_NAME}, {days} days left")


def _edge_listening(
    listeners: Sequence[Listener], available: bool, tailnet_address: str | None
) -> Check:
    """The bind is the access control (ADR 7, ADR 15), so a wildcard bind on
    80 or 443 is a failure, not a looser pass: it means the edge is published
    to the whole LAN, which is the one thing the host must never do, and
    nothing else reports it because the socket is accounted to the Traefik
    container."""
    if not available:
        return Check(CHECK_EDGE_LISTENING, STATE_UNKNOWN, "the socket table could not be read")
    if tailnet_address is None:
        return Check(CHECK_EDGE_LISTENING, STATE_UNKNOWN, "no tailnet address")
    wildcard = [
        port
        for port in EDGE_PORTS
        if any(
            listener.proto == PROTO_TCP and listener.port == port and listener.addr == ANY_ADDR
            for listener in listeners
        )
    ]
    if wildcard:
        ports = " and ".join(str(p) for p in wildcard)
        return Check(
            CHECK_EDGE_LISTENING,
            STATE_FAILED,
            f"the edge is published on 0.0.0.0:{ports}, not the tailnet address only",
        )
    missing = [
        port
        for port in EDGE_PORTS
        if not any(
            listener.proto == PROTO_TCP
            and listener.port == port
            and listener.addr == tailnet_address
            for listener in listeners
        )
    ]
    if missing:
        ports = " and ".join(str(p) for p in missing)
        return Check(
            CHECK_EDGE_LISTENING, STATE_FAILED, f"nothing listens on {tailnet_address}:{ports}"
        )
    ports = " and ".join(f":{p}" for p in EDGE_PORTS)
    return Check(CHECK_EDGE_LISTENING, STATE_OK, f"the edge listens on {tailnet_address}{ports}")
