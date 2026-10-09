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
