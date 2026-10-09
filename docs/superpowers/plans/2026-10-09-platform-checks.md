# Platform Checks and the Broken Banner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Judge the host's own machinery from what the status page already collects, write the verdict to a file both surfaces read, and take over the top of the status page and the tty1 console with a red banner while anything fails.

**Architecture:** A new pure policy module (`checks.py`) turns collected evidence into named checks. The web prober runs it every cycle, stores the result in the snapshot, and publishes it as JSON to `/run/harbor-console/checks.json` (`verdict.py` is the codec and file I/O). The console reads that file once per tick and never probes. One new collector (`certificate.py`) and one new probe (the page's own route through Traefik) supply the two pieces of evidence the page lacks today. A `harbor-console-check` command reads the same file so `install.sh` can end by failing loudly.

**Tech Stack:** Python 3.13, stdlib `ssl`/`json`/`socket`, `rich` (already a dependency), `pytest`, `uv`. No new runtime dependency.

Spec: `docs/superpowers/specs/2026-10-09-platform-checks-design.md`.

## Global Constraints

- No new runtime dependency. Stdlib `ssl` for the certificate, stdlib `json` for the file.
- Collectors never raise. `certificate.py` returns a sentinel with a reason.
- `unknown` never trips the banner. A check with missing evidence says so and stays out of the verdict.
- Platform only. A single declared service being down is a row-level `DOWN`, never a check.
- Colour only in the banner. Nothing else on either surface gets colour (ADR 21, written in Task 10).
- Thresholds are constants: certificate warning at 14 days, staleness at 90 s (three 30 s cycles).
- Verdict file path `/run/harbor-console/checks.json`; written atomically (temp name in the same directory, then `os.replace`).
- The console's 1 Hz loop never blocks: it reads one small file per tick and nothing else new.
- The console banner is exactly 3 rows at 80 columns. Healthy shows no banner at all.
- No new HTTP endpoint. The page stays the only thing the web service serves.
- One deviation from the spec's sketch, forced by imports: `run_checks` takes plain values (containers, routers, health, listeners...) rather than a `Snapshot`, because `snapshot.py` must import `Check` and a `Snapshot` parameter would make the two modules import each other. This is also how `directory.py` already works: plain values in, policy out.
- Every file keeps the project's module docstring style: say what the module does and why the non-obvious choices were made.
- Commit after every task with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` as the last line. Work on branch `feat/platform-checks`, which already exists and holds the spec.

---

## File map

| File | Responsibility |
|---|---|
| `src/harbor_console/checks.py` (new) | Policy, pure. `Check`, the check names and states, `run_checks`, `platform_broken`, `is_stale`, `freshness_check`. |
| `src/harbor_console/certificate.py` (new) | Collector. The certificate the edge serves on the tailnet address, or why it could not be read. |
| `src/harbor_console/verdict.py` (new) | Contract. `Verdict`, JSON `dumps`/`loads`, `read_verdict`, `write_verdict`, `VERDICT_PATH`. |
| `src/harbor_console/check.py` (new) | The `harbor-console-check` entry point. |
| `src/harbor_console/snapshot.py` | Gains `own_route`, `certificate`, `checks`. |
| `src/harbor_console/webapp.py` | `collect_snapshot` probes the own route and the certificate and runs the checks; `probe_loop` publishes the verdict; `VerdictPublisher` writes the file. |
| `src/harbor_console/web.py` | Red block when broken; footer line otherwise. |
| `src/harbor_console/ui.py` | `build_banner` and a `banner` parameter on `build_dashboard`. |
| `src/harbor_console/app.py` | `verdict_reader` and `clock` injectables; composes the banner. |
| `pyproject.toml` | Third console script. |
| `deploy/harbor-console-web.service` | `RuntimeDirectory=harbor-console`, `RuntimeDirectoryPreserve=yes`. |
| `deploy/install.sh` | Ends by polling `harbor-console-check`. |
| `tests/test_checks.py`, `tests/test_certificate.py`, `tests/test_verdict.py`, `tests/test_check.py` (new); `tests/test_webapp.py`, `tests/test_web.py`, `tests/test_ui.py`, `tests/test_app.py`, `tests/test_deploy.py` (modified) | Tests. |
| `docs/adr/0021-colour-the-platform-banner-and-nothing-else.md` (new), `docs/deployment.md`, `CLAUDE.md` | Record and describe. |

---

### Task 1: `checks.py` types, `platform_broken`, `is_stale`, `freshness_check`

**Files:**
- Create: `src/harbor_console/checks.py`
- Test: `tests/test_checks.py`

**Interfaces:**
- Produces: `Check(name: str, state: str, reason: str)` frozen dataclass; constants `STATE_OK = "ok"`, `STATE_FAILED = "failed"`, `STATE_UNKNOWN = "unknown"`; check names `CHECK_DOCKER = "docker"`, `CHECK_TRAEFIK_API = "traefik-api"`, `CHECK_DOCKER_PROVIDER = "docker-provider"`, `CHECK_OWN_ROUTE = "own-route"`, `CHECK_CERTIFICATE = "certificate"`, `CHECK_EDGE_LISTENING = "edge-listening"`, `CHECK_PROBER_FRESH = "prober-fresh"`; `OWN_ROUTE_HOST = "harbor.hpz440.ohr3023.org"`; `WILDCARD_NAME = "*.hpz440.ohr3023.org"`; `CERTIFICATE_MIN_DAYS = 14`; `STALE_AFTER_SECONDS = 90.0`; `platform_broken(checks) -> bool`; `is_stale(written: datetime, now: datetime) -> bool`; `freshness_check(written: datetime | None, now: datetime) -> Check`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_checks.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_checks.py -q`
Expected: `ImportError` / `ModuleNotFoundError: No module named 'harbor_console.checks'`

- [ ] **Step 3: Write the module**

Create `src/harbor_console/checks.py`:

```python
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
```

Leave `run_checks` for Task 3; the imports of `Certificate`, `Container`, `Router` and the others are used there. `certificate.py` does not exist yet, so for this task also create the stub `src/harbor_console/certificate.py` with only the two types (Task 2 fills in the collector):

```python
"""The certificate the edge serves on the tailnet address."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Certificate:
    """What the edge presented: every name it covers and when it expires (UTC)."""

    names: tuple[str, ...]
    not_after: datetime


@dataclass(frozen=True)
class CertificateUnavailable:
    """Why the certificate could not be read. Never raised, always returned."""

    reason: str
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_checks.py -q`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/checks.py src/harbor_console/certificate.py tests/test_checks.py
git commit -m "feat: platform check types, broken and stale judgements

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `certificate.py` collector

**Files:**
- Modify: `src/harbor_console/certificate.py`
- Test: `tests/test_certificate.py`

**Interfaces:**
- Consumes: `Certificate`, `CertificateUnavailable` from Task 1.
- Produces: `served_certificate(address: str, server_name: str = "harbor.hpz440.ohr3023.org", port: int = 443, timeout: float = 2.0, connector: Callable[[str, int, str, float], dict] = _connect) -> Certificate | CertificateUnavailable`; `CERTIFICATE_TIMEOUT_SECONDS = 2.0`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_certificate.py`:

```python
import socket
import ssl
from datetime import datetime, timezone

import pytest

from harbor_console.certificate import (
    Certificate,
    CertificateUnavailable,
    served_certificate,
)

PEER = {
    "subject": ((("commonName", "hpz440.ohr3023.org"),),),
    "subjectAltName": (("DNS", "hpz440.ohr3023.org"), ("DNS", "*.hpz440.ohr3023.org")),
    "notAfter": "Dec 18 12:05:54 2026 GMT",
}


def connector_returning(peer):
    seen = []

    def connect(address, port, server_name, timeout):
        seen.append((address, port, server_name, timeout))
        return peer

    return connect, seen


def test_reads_every_name_and_the_expiry():
    connect, _ = connector_returning(PEER)

    result = served_certificate("100.69.239.123", connector=connect)

    assert isinstance(result, Certificate)
    assert result.names == ("hpz440.ohr3023.org", "*.hpz440.ohr3023.org")
    assert result.not_after == datetime(2026, 12, 18, 12, 5, 54, tzinfo=timezone.utc)


def test_connects_to_the_address_with_the_route_hosts_sni():
    connect, seen = connector_returning(PEER)

    served_certificate("100.69.239.123", connector=connect)

    assert seen == [("100.69.239.123", 443, "harbor.hpz440.ohr3023.org", 2.0)]


def test_falls_back_to_the_subject_when_there_is_no_san():
    peer = {"subject": ((("commonName", "only.example"),),), "notAfter": PEER["notAfter"]}
    connect, _ = connector_returning(peer)

    result = served_certificate("100.69.239.123", connector=connect)

    assert isinstance(result, Certificate)
    assert result.names == ("only.example",)


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError("refused"),
        socket.timeout("timed out"),
        ssl.SSLCertVerificationError("certificate has expired"),
        ssl.SSLError("handshake failure"),
        OSError("network unreachable"),
    ],
)
def test_degrades_when_the_connection_fails(error):
    def connect(address, port, server_name, timeout):
        raise error

    result = served_certificate("100.69.239.123", connector=connect)

    assert isinstance(result, CertificateUnavailable)
    assert str(error).split("(")[0] in result.reason or type(error).__name__ in result.reason


def test_degrades_on_an_empty_peer_certificate():
    connect, _ = connector_returning({})

    result = served_certificate("100.69.239.123", connector=connect)

    assert isinstance(result, CertificateUnavailable)
    assert "no certificate" in result.reason


def test_degrades_on_an_unparseable_expiry():
    connect, _ = connector_returning({**PEER, "notAfter": "someday"})

    result = served_certificate("100.69.239.123", connector=connect)

    assert isinstance(result, CertificateUnavailable)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_certificate.py -q`
Expected: `ImportError: cannot import name 'served_certificate'`

- [ ] **Step 3: Write the collector**

Replace `src/harbor_console/certificate.py` with:

```python
"""The certificate the edge serves on the tailnet address.

Collects only. One TLS handshake to `<tailnet address>:443` with the page's
own route name as SNI, which is exactly what a browser on the tailnet does,
so what comes back is what every route is served with. Stdlib `ssl`, no new
dependency, a 2 s bound so a wedged edge cannot stall the prober.

Never raises. A handshake that fails for any reason -- refused, timed out,
expired, wrong name, not TLS at all -- is returned as `CertificateUnavailable`
with the reason, and the check in `checks.py` decides what that means. It
means failed: the edge not completing a handshake on its own address is
platform breakage, not missing evidence.
"""

from __future__ import annotations

import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

CERTIFICATE_TIMEOUT_SECONDS = 2.0
DEFAULT_SERVER_NAME = "harbor.hpz440.ohr3023.org"
TLS_PORT = 443


@dataclass(frozen=True)
class Certificate:
    """What the edge presented: every name it covers and when it expires (UTC)."""

    names: tuple[str, ...]
    not_after: datetime


@dataclass(frozen=True)
class CertificateUnavailable:
    """Why the certificate could not be read. Never raised, always returned."""

    reason: str


def _connect(address: str, port: int, server_name: str, timeout: float) -> dict:
    """Handshake and return `getpeercert()`. The one call that touches the network."""
    context = ssl.create_default_context()
    with socket.create_connection((address, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=server_name) as tls:
            return tls.getpeercert() or {}


def served_certificate(
    address: str,
    server_name: str = DEFAULT_SERVER_NAME,
    port: int = TLS_PORT,
    timeout: float = CERTIFICATE_TIMEOUT_SECONDS,
    connector: Callable[[str, int, str, float], dict] = _connect,
) -> Certificate | CertificateUnavailable:
    """The certificate served at `address:port` for `server_name`, or why not."""
    try:
        peer = connector(address, port, server_name, timeout)
    except (OSError, ValueError) as exc:
        # ssl.SSLError and socket.timeout are both OSError subclasses.
        return CertificateUnavailable(f"{exc.__class__.__name__}: {exc}")

    if not peer:
        return CertificateUnavailable("no certificate was presented")

    names = tuple(
        value for kind, value in peer.get("subjectAltName", ()) if kind == "DNS"
    )
    if not names:
        names = tuple(
            value
            for rdn in peer.get("subject", ())
            for key, value in rdn
            if key == "commonName"
        )

    try:
        not_after = datetime.fromtimestamp(
            ssl.cert_time_to_seconds(str(peer.get("notAfter", ""))), tz=timezone.utc
        )
    except (ValueError, OverflowError, OSError) as exc:
        return CertificateUnavailable(f"could not read the expiry: {exc}")

    return Certificate(names=names, not_after=not_after)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_certificate.py tests/test_checks.py -q`
Expected: `15 passed`

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/certificate.py tests/test_certificate.py
git commit -m "feat: collect the certificate the edge serves on the tailnet address

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `run_checks`

**Files:**
- Modify: `src/harbor_console/checks.py`
- Test: `tests/test_checks.py`

**Interfaces:**
- Consumes: Task 1 and Task 2 types; `declared_kind`, `KIND_HTTP`, `route_url` from `directory.py`; `Listener`, `PROTO_TCP`, `ANY_ADDR` from `listening.py`; `Router` from `traefik.py`; `Container` from `docker.py`; `Health` from `probe.py`.
- Produces: `run_checks(*, now: datetime, docker_available: bool, traefik_available: bool, containers: Sequence[Container], routers: Sequence[Router], own_route: Health | None, certificate: Certificate | CertificateUnavailable | None, listeners: Sequence[Listener], listeners_available: bool, tailnet_address: str | None) -> tuple[Check, ...]`, six checks in the order docker, traefik-api, docker-provider, own-route, certificate, edge-listening.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_checks.py`:

```python
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


def test_edge_listening_accepts_a_wildcard_bind():
    check = checks(listeners=(Listener("0.0.0.0", 80, 1), Listener("0.0.0.0", 443, 1)))[CHECK_EDGE_LISTENING]

    assert check.state == STATE_OK


def test_edge_listening_ignores_udp_on_the_same_ports():
    udp = (Listener(TAILNET, 80, 1, "udp"), Listener(TAILNET, 443, 1, "udp"))

    assert checks(listeners=udp)[CHECK_EDGE_LISTENING].state == STATE_FAILED


def test_edge_listening_is_unknown_without_the_socket_table():
    assert checks(listeners_available=False)[CHECK_EDGE_LISTENING].state == STATE_UNKNOWN


def test_edge_listening_is_unknown_without_a_tailnet_address():
    assert checks(tailnet_address=None)[CHECK_EDGE_LISTENING].state == STATE_UNKNOWN
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_checks.py -q`
Expected: `ImportError: cannot import name 'run_checks'`

- [ ] **Step 3: Write `run_checks`**

Append to `src/harbor_console/checks.py`:

```python
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
    if not available:
        return Check(CHECK_EDGE_LISTENING, STATE_UNKNOWN, "the socket table could not be read")
    if tailnet_address is None:
        return Check(CHECK_EDGE_LISTENING, STATE_UNKNOWN, "no tailnet address")
    missing = [
        port
        for port in EDGE_PORTS
        if not any(
            l.proto == PROTO_TCP and l.port == port and l.addr in (tailnet_address, ANY_ADDR)
            for l in listeners
        )
    ]
    if missing:
        ports = " and ".join(str(p) for p in missing)
        return Check(
            CHECK_EDGE_LISTENING, STATE_FAILED, f"nothing listens on {tailnet_address}:{ports}"
        )
    return Check(CHECK_EDGE_LISTENING, STATE_OK, f"the edge listens on {tailnet_address}:80 and :443")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_checks.py -q`
Expected: `28 passed`

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/checks.py tests/test_checks.py
git commit -m "feat: judge the platform from one cycle's evidence

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `verdict.py` contract, codec and file I/O

**Files:**
- Create: `src/harbor_console/verdict.py`
- Test: `tests/test_verdict.py`

**Interfaces:**
- Consumes: `Check` from Task 1.
- Produces: `Verdict(written: datetime, hostname: str, checks: tuple[Check, ...])`; `VERDICT_PATH = Path("/run/harbor-console/checks.json")`; `dumps(verdict) -> str`; `loads(text: str) -> Verdict | None`; `read_verdict(path: Path = VERDICT_PATH) -> Verdict | None`; `write_verdict(verdict, path: Path = VERDICT_PATH) -> None` (raises `OSError` on failure, the caller decides).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_verdict.py`:

```python
import json
from datetime import datetime

import pytest

from harbor_console.checks import STATE_FAILED, STATE_OK, Check
from harbor_console.verdict import (
    VERDICT_PATH,
    Verdict,
    dumps,
    loads,
    read_verdict,
    write_verdict,
)

VERDICT = Verdict(
    written=datetime(2026, 10, 9, 17, 21, 46),
    hostname="hpz440",
    checks=(
        Check("docker", STATE_OK, "Docker answered"),
        Check("own-route", STATE_FAILED, "https://harbor.hpz440.ohr3023.org/ did not answer"),
    ),
)


def test_round_trip():
    assert loads(dumps(VERDICT)) == VERDICT


def test_dumps_is_plain_json_with_isoformat_timestamp():
    payload = json.loads(dumps(VERDICT))

    assert payload["written"] == "2026-10-09T17:21:46"
    assert payload["hostname"] == "hpz440"
    assert payload["checks"][1] == {
        "name": "own-route",
        "state": "failed",
        "reason": "https://harbor.hpz440.ohr3023.org/ did not answer",
    }


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        '{"written": "2026-10-09T17:21:46"}',
        '{"written": "yesterday", "hostname": "h", "checks": []}',
        '{"written": "2026-10-09T17:21:46", "hostname": "h", "checks": [{"name": "x"}]}',
        '{"written": "2026-10-09T17:21:46", "hostname": "h", "checks": "none"}',
    ],
)
def test_loads_returns_none_for_malformed_input(text):
    assert loads(text) is None


def test_write_then_read(tmp_path):
    path = tmp_path / "checks.json"

    write_verdict(VERDICT, path)

    assert read_verdict(path) == VERDICT


def test_write_leaves_no_temp_file_behind(tmp_path):
    path = tmp_path / "checks.json"

    write_verdict(VERDICT, path)

    assert [p.name for p in tmp_path.iterdir()] == ["checks.json"]


def test_write_raises_when_the_directory_is_missing(tmp_path):
    with pytest.raises(OSError):
        write_verdict(VERDICT, tmp_path / "missing" / "checks.json")


def test_read_returns_none_when_the_file_is_missing(tmp_path):
    assert read_verdict(tmp_path / "checks.json") is None


def test_read_returns_none_on_garbage(tmp_path):
    path = tmp_path / "checks.json"
    path.write_text("{", encoding="utf-8")

    assert read_verdict(path) is None


def test_the_default_path_is_under_run():
    assert str(VERDICT_PATH) == "/run/harbor-console/checks.json"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_verdict.py -q`
Expected: `ModuleNotFoundError: No module named 'harbor_console.verdict'`

- [ ] **Step 3: Write the module**

Create `src/harbor_console/verdict.py`:

```python
"""The verdict file: how the checks cross from the web prober to the console.

A contract, like `snapshot.py`, with the one addition that this one crosses
a process boundary, so it carries its own codec and the two file calls.
No policy: what the checks mean is `checks.py`'s business, and what to show
is the renderers'.

The file lives under `/run`, a tmpfs the web unit owns through systemd's
`RuntimeDirectory`, so a reboot starts clean and nothing persists a verdict
from a previous life of the host. Written atomically -- a temp name in the
same directory, then `os.replace` -- so a reader never sees half a file.
`loads` and `read_verdict` never raise: a missing, unreadable or malformed
file is `None`, and every reader treats `None` as "nothing has reported".
`write_verdict` does raise, because its one caller decides how to report a
write that failed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from harbor_console.checks import Check

VERDICT_PATH = Path("/run/harbor-console/checks.json")


@dataclass(frozen=True)
class Verdict:
    """One cycle's checks, stamped with when the prober wrote them."""

    written: datetime
    hostname: str
    checks: tuple[Check, ...]


def dumps(verdict: Verdict) -> str:
    return json.dumps(
        {
            "written": verdict.written.isoformat(),
            "hostname": verdict.hostname,
            "checks": [
                {"name": c.name, "state": c.state, "reason": c.reason} for c in verdict.checks
            ],
        },
        indent=2,
    )


def loads(text: str) -> Verdict | None:
    """Parse a verdict, or None for anything that is not one. Never raises."""
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        written = datetime.fromisoformat(str(payload["written"]))
        hostname = str(payload["hostname"])
        raw_checks = payload["checks"]
        if not isinstance(raw_checks, list):
            return None
        checks = tuple(
            Check(name=str(c["name"]), state=str(c["state"]), reason=str(c["reason"]))
            for c in raw_checks
        )
    except (KeyError, TypeError, ValueError):
        return None
    return Verdict(written=written, hostname=hostname, checks=checks)


def read_verdict(path: Path = VERDICT_PATH) -> Verdict | None:
    """The verdict on disk, or None. Never raises; never blocks on anything but a local file."""
    try:
        return loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def write_verdict(verdict: Verdict, path: Path = VERDICT_PATH) -> None:
    """Replace the file atomically. Raises OSError; the caller reports it."""
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(dumps(verdict), encoding="utf-8")
    os.replace(temp, path)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_verdict.py -q`
Expected: `15 passed`

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/verdict.py tests/test_verdict.py
git commit -m "feat: the verdict file contract, codec and atomic write

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Snapshot fields and `collect_snapshot` running the checks

**Files:**
- Modify: `src/harbor_console/snapshot.py`
- Modify: `src/harbor_console/webapp.py` (`collect_snapshot`)
- Test: `tests/test_webapp.py`

**Interfaces:**
- Consumes: `run_checks`, `OWN_ROUTE_HOST`, `Check` (Task 3); `served_certificate`, `Certificate`, `CertificateUnavailable` (Task 2).
- Produces: `Snapshot.own_route: Health | None = None`, `Snapshot.certificate: Certificate | CertificateUnavailable | None = None`, `Snapshot.checks: tuple[Check, ...] = ()`; `collect_snapshot(..., certificate: Callable[[str], Certificate | CertificateUnavailable] = served_certificate, ...)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_webapp.py` (the `collect` helper, `HOST`, `PARKSMART`, `ROUTER`, `NOW` already exist at the top of the file):

```python
from datetime import timezone as _tz

from harbor_console.certificate import Certificate, CertificateUnavailable
from harbor_console.checks import (
    CHECK_CERTIFICATE,
    CHECK_DOCKER_PROVIDER,
    CHECK_OWN_ROUTE,
    STATE_FAILED,
    STATE_OK,
    STATE_UNKNOWN,
)

GOOD_CERT = Certificate(
    ("hpz440.ohr3023.org", "*.hpz440.ohr3023.org"),
    datetime(2026, 12, 18, tzinfo=_tz.utc),
)


def test_collect_snapshot_probes_the_pages_own_route_through_the_proxy():
    seen = []

    def prober(url):
        seen.append(url)
        return Health(True, None, None, (), None)

    snapshot = collect(prober=prober, certificate=lambda address: GOOD_CERT)

    assert "https://harbor.hpz440.ohr3023.org/" in seen
    assert snapshot.own_route is not None and snapshot.own_route.up is True


def test_collect_snapshot_does_not_probe_its_own_route_without_a_tailnet_address():
    seen = []

    def prober(url):
        seen.append(url)
        return Health(True, None, None, (), None)

    snapshot = collect(prober=prober, tailnet_address=None, certificate=lambda a: GOOD_CERT)

    assert "https://harbor.hpz440.ohr3023.org/" not in seen
    assert snapshot.own_route is None


def test_collect_snapshot_reads_the_certificate_at_the_tailnet_address():
    seen = []

    def certificate(address):
        seen.append(address)
        return GOOD_CERT

    snapshot = collect(certificate=certificate)

    assert seen == ["100.69.239.123"]
    assert snapshot.certificate == GOOD_CERT


def test_collect_snapshot_skips_the_certificate_without_a_tailnet_address():
    snapshot = collect(tailnet_address=None, certificate=lambda a: GOOD_CERT)

    assert snapshot.certificate is None


def test_collect_snapshot_defaults_to_the_real_certificate_collector():
    assert (
        inspect.signature(webapp.collect_snapshot).parameters["certificate"].default
        is webapp.served_certificate
    )


def test_collect_snapshot_runs_the_checks():
    snapshot = collect(certificate=lambda a: GOOD_CERT)

    states = {c.name: c.state for c in snapshot.checks}
    assert states[CHECK_DOCKER_PROVIDER] == STATE_OK
    assert states[CHECK_OWN_ROUTE] == STATE_OK
    assert states[CHECK_CERTIFICATE] == STATE_OK


def test_collect_snapshot_checks_fail_when_the_docker_provider_is_dead():
    file_only = Router("harbor@file", "harbor.hpz440.ohr3023.org", "harbor", True, None)

    snapshot = collect(routers=lambda: (file_only,), certificate=lambda a: GOOD_CERT)

    states = {c.name: c.state for c in snapshot.checks}
    assert states[CHECK_DOCKER_PROVIDER] == STATE_FAILED


def test_collect_snapshot_checks_fail_on_a_failed_handshake():
    snapshot = collect(certificate=lambda a: CertificateUnavailable("refused"))

    states = {c.name: c.state for c in snapshot.checks}
    assert states[CHECK_CERTIFICATE] == STATE_FAILED


def test_the_starting_snapshot_has_no_checks():
    snapshot = webapp.starting_snapshot("h", NOW)

    assert snapshot.checks == ()
    assert snapshot.own_route is None
    assert snapshot.certificate is None
```

Also update the existing `collect` helper so every existing test keeps passing without touching the network: add `certificate=lambda address: GOOD_CERT` to its `kwargs`. `GOOD_CERT` must therefore be defined above `collect`; move the new imports and `GOOD_CERT` to the top of the file next to the existing imports.

Three existing tests assert exactly which URLs the prober saw, and the page's own route is now always among them. Change them so they still test what they tested:

```python
def test_http_rows_are_probed_at_their_route():
    seen = []

    def prober(url):
        seen.append(url)
        return Health(True, None, None, (), None)

    collect(prober=prober)

    assert f"https://{HOST}/" in seen


def test_rows_without_a_host_are_not_probed():
    seen = []
    plain = Container("plain", (), {"traefik.enable": "true"})

    collect(containers=lambda: (plain,), routers=lambda: (), prober=lambda url: seen.append(url))

    # Only the page's own route; nothing for the container with no host.
    assert seen == ["https://harbor.hpz440.ohr3023.org/"]


def test_non_http_rows_are_not_probed():
    seen = []
    mqtt = Container("mqtt", (("0.0.0.0", 1883),), {"harbor.kind": "tcp", "harbor.port": "1883"})

    collect(containers=lambda: (mqtt,), routers=lambda: (), prober=lambda url: seen.append(url))

    assert seen == ["https://harbor.hpz440.ohr3023.org/"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_webapp.py -q`
Expected: failures with `TypeError: collect_snapshot() got an unexpected keyword argument 'certificate'` and `cannot import name 'Certificate'`-style errors until the code exists.

- [ ] **Step 3: Add the snapshot fields**

In `src/harbor_console/snapshot.py`, add the imports:

```python
from harbor_console.certificate import Certificate, CertificateUnavailable
from harbor_console.checks import Check
```

and append these fields to the end of the `Snapshot` dataclass:

```python
    #: The page probed at its own route through Traefik -- the path ADR 17
    #: and ADR 19 broke, and the one route the directory does not cover
    #: because the page is not a container. None when not attempted (no
    #: tailnet address), which the checks report as unknown, not failed.
    own_route: Health | None = None
    #: The certificate the edge served on the tailnet address, or why it
    #: could not be read. None when not attempted.
    certificate: Certificate | CertificateUnavailable | None = None
    #: The platform checks judged from this cycle (`checks.run_checks`).
    #: Empty until the first cycle. The renderer adds the prober-fresh
    #: check itself, from `collected`, because the prober cannot report
    #: its own silence.
    checks: tuple[Check, ...] = ()
```

- [ ] **Step 4: Extend `collect_snapshot`**

In `src/harbor_console/webapp.py`, add imports:

```python
from harbor_console.certificate import Certificate, CertificateUnavailable, served_certificate
from harbor_console.checks import OWN_ROUTE_HOST, run_checks
```

Change the `collect_snapshot` signature to add one parameter after `gpus`:

```python
    certificate: Callable[[str], Certificate | CertificateUnavailable] = served_certificate,
```

and replace the body from `health: dict[str, Health] = {}` to the end of the function with:

```python
    health: dict[str, Health] = {}
    for container in running:
        if declared_kind(container) != KIND_HTTP:
            continue
        route = route_of(container)
        if route is None or route[1] is None:
            continue
        health[route[0]] = prober(route_url(route[1]))

    # The page's own route and the served certificate both need the edge's
    # address; without one they are not attempted, and the checks say so.
    own_route = prober(route_url(OWN_ROUTE_HOST)) if tailnet_address is not None else None
    served = certificate(tailnet_address) if tailnet_address is not None else None

    docker_available = running is not DOCKER_UNAVAILABLE
    traefik_available = routed is not TRAEFIK_UNAVAILABLE
    listeners_available = found is not LISTENING_UNAVAILABLE

    return Snapshot(
        collected=now,
        metrics=metrics,
        rows=build_rows(running, routed, found, health, probed=True),
        findings=find_findings(running, routed, found, tailnet_address, own_port=own_port),
        inventory=build_inventory(found, running, tailnet_address, own_port),
        containers=tuple(running),
        docker_available=docker_available,
        traefik_available=traefik_available,
        listeners_available=listeners_available,
        health=health,
        collection_error=None,
        probed=True,
        tailnet_address=tailnet_address,
        storage=storage(),
        gpus=gpus(),
        own_route=own_route,
        certificate=served,
        checks=run_checks(
            now=now,
            docker_available=docker_available,
            traefik_available=traefik_available,
            containers=running,
            routers=routed,
            own_route=own_route,
            certificate=served,
            listeners=found,
            listeners_available=listeners_available,
            tailnet_address=tailnet_address,
        ),
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_webapp.py tests/test_web.py -q`
Expected: all pass (the existing count plus 9 new).

- [ ] **Step 6: Commit**

```bash
git add src/harbor_console/snapshot.py src/harbor_console/webapp.py tests/test_webapp.py
git commit -m "feat: probe the page's own route, read the certificate, run the checks each cycle

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Publish the verdict from the prober

**Files:**
- Modify: `src/harbor_console/webapp.py` (`probe_loop`, `VerdictPublisher`, `_default_prober`)
- Test: `tests/test_webapp.py`

**Interfaces:**
- Consumes: `Verdict`, `write_verdict`, `VERDICT_PATH` (Task 4).
- Produces: `probe_loop(holder, collect, sleep=time.sleep, interval=PROBE_INTERVAL_SECONDS, publish: Callable[[Snapshot], None] | None = None)`; `class VerdictPublisher` with `__init__(self, path: Path = VERDICT_PATH, writer=write_verdict, report: Callable[[str], None] = _report)` and `__call__(self, snapshot: Snapshot) -> None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_webapp.py`:

```python
from harbor_console.checks import Check
from harbor_console.verdict import Verdict, read_verdict


def test_probe_loop_publishes_each_good_snapshot():
    holder = webapp.SnapshotHolder(Snapshot(collected=datetime(2026, 1, 1), metrics=METRICS))
    published = []

    def collect_fn():
        return Snapshot(collected=datetime(2026, 9, 2), metrics=METRICS)

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    webapp.probe_loop(holder, collect=collect_fn, sleep=fake_sleep, publish=published.append)

    assert [s.collected for s in published] == [datetime(2026, 9, 2)]


def test_probe_loop_does_not_publish_after_a_failed_cycle():
    holder = webapp.SnapshotHolder(Snapshot(collected=datetime(2026, 1, 1), metrics=METRICS))
    published = []

    def collect_fn():
        raise RuntimeError("psutil fell over")

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    webapp.probe_loop(holder, collect=collect_fn, sleep=fake_sleep, publish=published.append)

    assert published == []


def test_probe_loop_survives_a_publish_that_raises():
    holder = webapp.SnapshotHolder(Snapshot(collected=datetime(2026, 1, 1), metrics=METRICS))

    def collect_fn():
        return Snapshot(collected=datetime(2026, 9, 2), metrics=METRICS)

    def publish(_snapshot):
        raise OSError("read-only")

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    webapp.probe_loop(holder, collect=collect_fn, sleep=fake_sleep, publish=publish)

    assert holder.get().collected == datetime(2026, 9, 2)


def test_verdict_publisher_writes_the_snapshots_checks(tmp_path):
    path = tmp_path / "checks.json"
    checks = (Check("docker", "ok", "Docker answered"),)
    snapshot = Snapshot(collected=datetime(2026, 9, 2, 1, 2, 3), metrics=METRICS, checks=checks)

    webapp.VerdictPublisher(path)(snapshot)

    assert read_verdict(path) == Verdict(datetime(2026, 9, 2, 1, 2, 3), "hpz440", checks)


def test_verdict_publisher_reports_a_write_failure_once_per_distinct_error(tmp_path):
    reported = []
    publisher = webapp.VerdictPublisher(tmp_path / "missing" / "checks.json", report=reported.append)
    snapshot = Snapshot(collected=datetime(2026, 9, 2), metrics=METRICS)

    publisher(snapshot)
    publisher(snapshot)

    assert len(reported) == 1
    assert "checks.json" in reported[0]


def test_verdict_publisher_reports_again_after_a_different_error(tmp_path):
    reported = []
    errors = iter([OSError("first"), OSError("second")])

    def writer(_verdict, _path):
        raise next(errors)

    publisher = webapp.VerdictPublisher(tmp_path / "checks.json", writer=writer, report=reported.append)
    snapshot = Snapshot(collected=datetime(2026, 9, 2), metrics=METRICS)

    publisher(snapshot)
    publisher(snapshot)

    assert len(reported) == 2


def test_the_default_prober_publishes_to_the_verdict_path():
    assert (
        inspect.signature(webapp.VerdictPublisher.__init__).parameters["path"].default
        is webapp.VERDICT_PATH
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_webapp.py -q -k "publish or VerdictPublisher or verdict_path"`
Expected: `TypeError: probe_loop() got an unexpected keyword argument 'publish'` and `AttributeError: module 'harbor_console.webapp' has no attribute 'VerdictPublisher'`.

- [ ] **Step 3: Implement**

In `src/harbor_console/webapp.py`, add imports:

```python
from harbor_console.verdict import VERDICT_PATH, Verdict, write_verdict
```

Replace `probe_loop` with:

```python
def probe_loop(
    holder: SnapshotHolder,
    collect: Callable[[], Snapshot],
    sleep: Callable[[float], None] = time.sleep,
    interval: float = PROBE_INTERVAL_SECONDS,
    publish: Callable[[Snapshot], None] | None = None,
) -> None:
    """Publish a fresh snapshot on an interval until interrupted.

    A collection failure never takes the page down: the last good snapshot
    stands, with the reason attached. A successful cycle publishes
    `collection_error=None`, so a reason never outlives its cause.

    `publish` is how the verdict leaves this process (the file the console
    reads). It runs only after a good cycle, so a failed cycle lets the
    file go stale rather than restating a verdict from evidence the cycle
    did not have; and it is guarded, so a write that fails never stops the
    loop either.
    """
    while True:
        try:
            snapshot = collect()
        except Exception as exc:  # noqa: BLE001 - a supervisor loop, see above
            holder.set(replace(holder.get(), collection_error=str(exc) or exc.__class__.__name__))
        else:
            holder.set(snapshot)
            if publish is not None:
                try:
                    publish(snapshot)
                except Exception:  # noqa: BLE001 - the publisher reports its own failures
                    pass
        try:
            sleep(interval)
        except KeyboardInterrupt:
            return
```

Add, after `probe_loop`:

```python
def _report(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class VerdictPublisher:
    """Write one snapshot's checks to the verdict file.

    A write that fails is reported once per distinct error, not once per
    cycle: the same message every 30 s to the journal is noise, and a
    different one is news. The loop never sees the exception.
    """

    def __init__(
        self,
        path: Path = VERDICT_PATH,
        writer: Callable[[Verdict, Path], None] = write_verdict,
        report: Callable[[str], None] = _report,
    ) -> None:
        self._path = path
        self._writer = writer
        self._report = report
        self._last_error: str | None = None

    def __call__(self, snapshot: Snapshot) -> None:
        verdict = Verdict(
            written=snapshot.collected,
            hostname=str(snapshot.metrics.get("hostname", "")),
            checks=snapshot.checks,
        )
        try:
            self._writer(verdict, self._path)
        except OSError as exc:
            message = f"could not write {self._path}: {exc}"
            if message != self._last_error:
                self._report(message)
                self._last_error = message
            return
        self._last_error = None
```

In `_default_prober`, pass the publisher:

```python
    thread = threading.Thread(
        target=probe_loop,
        args=(holder, collect),
        kwargs={"publish": VerdictPublisher()},
        name="harbor-prober",
        daemon=True,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_webapp.py -q`
Expected: all pass (existing plus 7 new).

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/webapp.py tests/test_webapp.py
git commit -m "feat: publish each cycle's verdict to /run/harbor-console/checks.json

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: The page's red block and footer line

**Files:**
- Modify: `src/harbor_console/web.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `platform_broken`, `freshness_check`, `STATE_FAILED`, `STATE_UNKNOWN` (Task 1); `Snapshot.checks`, `Snapshot.collected`, `Snapshot.probed`.
- Produces: `render_page(snapshot: Snapshot, now: datetime | None = None) -> bytes`; `page_checks(snapshot, now) -> tuple[Check, ...]` (the snapshot's checks plus prober-fresh).

- [ ] **Step 1: Write the failing tests**

`tests/test_web.py` already defines `METRICS`, `NOW` (2026-09-02) and a `snapshot(**overrides)` helper that builds a probed `Snapshot`; use those. The new tests use their own `CHECK_NOW` so the stale case has a known clock. Append:

```python
from datetime import timedelta

from harbor_console.checks import STATE_FAILED, STATE_OK, STATE_UNKNOWN, Check

CHECK_NOW = datetime(2026, 10, 9, 17, 21, 46)


def test_page_shows_a_red_block_when_a_check_failed():
    checks = (
        Check("docker", STATE_OK, "Docker answered"),
        Check("docker-provider", STATE_FAILED, "8 containers declare a route but Traefik reports no @docker router"),
    )

    page = web.render_page(snapshot(collected=CHECK_NOW, checks=checks), now=CHECK_NOW).decode()

    assert "PLATFORM BROKEN" in page
    assert 'class="broken"' in page
    assert "docker-provider: 8 containers declare a route" in page
    assert page.index("PLATFORM BROKEN") < page.index("<h2>")


def test_page_block_escapes_the_reason():
    checks = (Check("own-route", STATE_FAILED, "<b>504</b>"),)

    page = web.render_page(snapshot(collected=CHECK_NOW, checks=checks), now=CHECK_NOW).decode()

    assert "&lt;b&gt;504&lt;/b&gt;" in page


def test_page_shows_no_block_when_nothing_failed():
    checks = (Check("docker", STATE_OK, "Docker answered"),)

    page = web.render_page(snapshot(collected=CHECK_NOW, checks=checks), now=CHECK_NOW).decode()

    assert "PLATFORM BROKEN" not in page


def test_page_footer_counts_passed_checks_including_freshness():
    checks = (
        Check("docker", STATE_OK, "Docker answered"),
        Check("certificate", STATE_UNKNOWN, "not checked: no tailnet address"),
    )

    page = web.render_page(snapshot(collected=CHECK_NOW, checks=checks), now=CHECK_NOW).decode()

    assert "Platform checks: 2 passed, 1 unknown: certificate" in page


def test_page_block_when_the_prober_has_gone_quiet():
    checks = (Check("docker", STATE_OK, "Docker answered"),)
    quiet = snapshot(collected=CHECK_NOW - timedelta(minutes=5), checks=checks)

    page = web.render_page(quiet, now=CHECK_NOW).decode()

    assert "PLATFORM BROKEN" in page
    assert "prober-fresh: status page has not reported since 17:16:46" in page


def test_page_before_the_first_cycle_shows_no_block():
    page = web.render_page(Snapshot(collected=CHECK_NOW, metrics=METRICS), now=CHECK_NOW).decode()

    assert "PLATFORM BROKEN" not in page
    assert "Platform checks: 0 passed, 1 unknown: prober-fresh" in page


def test_the_handler_renders_with_the_current_time():
    import inspect

    assert inspect.signature(web.render_page).parameters["now"].default is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web.py -q`
Expected: `TypeError: render_page() got an unexpected keyword argument 'now'`

- [ ] **Step 3: Implement**

In `src/harbor_console/web.py`:

Add imports:

```python
from datetime import datetime

from harbor_console.checks import (
    STATE_FAILED,
    STATE_OK,
    STATE_UNKNOWN,
    Check,
    freshness_check,
    platform_broken,
)
```

Add to `_STYLE` after the `.banner` line:

```
.broken { background: #b00020; color: #fff; font-weight: 700; padding: 1rem 1.25rem; margin-bottom: 1.5rem; }
.broken p { margin: 0.25rem 0; }
.broken .headline { font-size: 1.5rem; letter-spacing: 0.05em; }
```

Change the signature to `def render_page(snapshot: Snapshot, now: datetime | None = None) -> bytes:` and insert, right after the `<h1>` part is appended and before the `collection_error` banner:

```python
    checks = page_checks(snapshot, now if now is not None else datetime.now())
    if platform_broken(checks):
        parts.append(_broken_block(checks))
```

Replace the footer `parts.append(...)` with:

```python
    parts.append(
        f"<p class=\"stamp\">Collected "
        f"{escape(snapshot.collected.strftime('%Y-%m-%d %H:%M:%S'))}, "
        f"refreshing every {REFRESH_SECONDS}s. {_checks_summary(checks)}</p>"
    )
```

Add these functions after `render_page`:

```python
def page_checks(snapshot: Snapshot, now: datetime) -> tuple[Check, ...]:
    """The snapshot's checks plus prober-fresh, which only a reader can judge.

    Before the first cycle there is nothing to judge from, and that is
    unknown, not failed: a page that has been up for two seconds is not a
    broken platform.
    """
    written = snapshot.collected if snapshot.probed else None
    return snapshot.checks + (freshness_check(written, now),)


def _broken_block(checks: tuple[Check, ...]) -> str:
    """The takeover. Red, above everything, one line per failed check.

    The only colour on the page (ADR 21): it means the host's own machinery
    is broken, never that a project is.
    """
    lines = "".join(
        f"<p>{escape(c.name)}: {escape(c.reason)}</p>" for c in checks if c.state == STATE_FAILED
    )
    return f'<div class="broken"><p class="headline">PLATFORM BROKEN</p>{lines}</div>'


def _checks_summary(checks: tuple[Check, ...]) -> str:
    """The footer's proof that the checks ran: counts, and which were unknown."""
    passed = sum(1 for c in checks if c.state == STATE_OK)
    unknown = [c.name for c in checks if c.state == STATE_UNKNOWN]
    summary = f"Platform checks: {passed} passed"
    if unknown:
        summary += f", {len(unknown)} unknown: {escape(', '.join(unknown))}"
    return summary + "."
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web.py tests/test_webapp.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/web.py tests/test_web.py
git commit -m "feat: the status page takes over with a red block while the platform is broken

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: The console banner

**Files:**
- Modify: `src/harbor_console/ui.py`
- Modify: `src/harbor_console/app.py`
- Test: `tests/test_ui.py`, `tests/test_app.py`

**Interfaces:**
- Consumes: `Verdict`, `read_verdict` (Task 4); `platform_broken`, `is_stale`, `STATE_FAILED` (Task 1).
- Produces: `ui.build_banner(verdict: Verdict | None, now: datetime, missing_since: datetime) -> Text | None` (exactly 3 lines when not None); `ui.build_dashboard(metrics, storage=(), gpus=(), banner: Text | None = None) -> Panel | Group`; `app.run(..., verdict_reader: Callable[[], Verdict | None] = read_verdict, clock: Callable[[], datetime] = datetime.now)`; the renderer injectable's signature becomes `renderer(metrics, storage, gpus, banner)`.

- [ ] **Step 1: Write the failing UI tests**

Append to `tests/test_ui.py`:

```python
from datetime import datetime, timedelta

from rich.console import Console as _Console

from harbor_console.checks import STATE_FAILED, STATE_OK, Check
from harbor_console.ui import build_banner
from harbor_console.verdict import Verdict

NOW = datetime(2026, 10, 9, 17, 21, 46)
STARTED = NOW - timedelta(minutes=10)


def verdict(checks, written=NOW):
    return Verdict(written=written, hostname="host-a", checks=tuple(checks))


def banner_lines(banner):
    console = _Console(width=80, record=True)
    console.print(banner)
    return console.export_text().splitlines()


def test_no_banner_when_nothing_failed():
    assert build_banner(verdict([Check("docker", STATE_OK, "answered")]), NOW, STARTED) is None


def test_banner_names_the_failed_checks_on_three_rows():
    checks = [
        Check("docker", STATE_OK, "answered"),
        Check("docker-provider", STATE_FAILED, "8 containers declare a route but Traefik reports no @docker router"),
        Check("own-route", STATE_FAILED, "https://harbor.hpz440.ohr3023.org/ did not answer through the proxy"),
    ]

    lines = banner_lines(build_banner(verdict(checks), NOW, STARTED))

    assert len(lines) == 3
    assert lines[0].startswith("PLATFORM BROKEN")
    assert lines[1].startswith("docker-provider: 8 containers")
    assert lines[2].startswith("own-route: https://harbor")
    assert all(len(line) <= 80 for line in lines)


def test_banner_pads_a_single_failure_to_three_rows():
    lines = banner_lines(build_banner(verdict([Check("docker", STATE_FAILED, "could not be read")]), NOW, STARTED))

    assert len(lines) == 3
    assert lines[1].startswith("docker: could not be read")


def test_banner_collapses_a_third_failure_and_beyond():
    checks = [Check(f"c{i}", STATE_FAILED, "x") for i in range(5)]

    lines = banner_lines(build_banner(verdict(checks), NOW, STARTED))

    assert lines[1].startswith("c0: x")
    assert "+4 more, see the status page" in lines[2]


def test_banner_when_the_verdict_is_stale():
    stale = verdict([Check("docker", STATE_OK, "answered")], written=NOW - timedelta(minutes=5))

    lines = banner_lines(build_banner(stale, NOW, STARTED))

    assert len(lines) == 3
    assert lines[1].startswith("status page has not reported since 17:16:46")


def test_banner_when_the_file_is_missing_long_after_start():
    lines = banner_lines(build_banner(None, NOW, STARTED))

    assert lines[1].startswith("status page has not reported since 17:11:46")


def test_no_banner_while_the_file_is_missing_just_after_start():
    assert build_banner(None, NOW, NOW - timedelta(seconds=30)) is None


def test_dashboard_puts_the_banner_above_the_panel_within_80_columns():
    banner = build_banner(verdict([Check("docker", STATE_FAILED, "could not be read")]), NOW, STARTED)

    console = _Console(width=80, record=True)
    console.print(build_dashboard(METRICS, (), (), banner))
    lines = console.export_text().splitlines()

    assert lines[0].startswith("PLATFORM BROKEN")
    assert "Harbor Console" in lines[3]
    assert any("Docker containers" in line and "17" in line for line in lines)


def test_dashboard_without_a_banner_is_unchanged():
    console = _Console(width=80, record=True)
    console.print(build_dashboard(METRICS, (), ()))
    lines = console.export_text().splitlines()

    assert "Harbor Console" in lines[0]
```

- [ ] **Step 2: Write the failing app tests**

In `tests/test_app.py`, every existing fake `renderer` takes three positional parameters; change each to take four (`def renderer(metrics, storage, gpus, banner):`, with underscores where unused) so they match the new signature. Then append:

```python
from datetime import datetime

from harbor_console.checks import STATE_FAILED, Check
from harbor_console.verdict import Verdict


def test_run_reads_the_verdict_every_tick_and_passes_a_banner(monkeypatch):
    seen = {}
    now = datetime(2026, 10, 9, 17, 21, 46)
    broken = Verdict(now, "h", (Check("docker", STATE_FAILED, "could not be read"),))

    def renderer(_metrics, _storage, _gpus, banner):
        seen["banner"] = banner
        return "rendered"

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "Live", DummyLive)

    app.run(
        collector=lambda: {"tick": 1},
        renderer=renderer,
        sleep=fake_sleep,
        storage_collector=lambda: (),
        gpu_collector=lambda: (),
        verdict_reader=lambda: broken,
        clock=lambda: now,
    )

    assert seen["banner"] is not None


def test_run_passes_no_banner_when_healthy(monkeypatch):
    seen = {}

    def renderer(_metrics, _storage, _gpus, banner):
        seen["banner"] = banner
        return "rendered"

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "Live", DummyLive)

    app.run(
        collector=lambda: {"tick": 1},
        renderer=renderer,
        sleep=fake_sleep,
        storage_collector=lambda: (),
        gpu_collector=lambda: (),
        verdict_reader=lambda: None,
        clock=lambda: datetime(2026, 10, 9, 17, 21, 46),
    )

    assert seen["banner"] is None


def test_run_defaults_to_the_real_verdict_reader():
    import inspect

    from harbor_console.verdict import read_verdict

    assert inspect.signature(app.run).parameters["verdict_reader"].default is read_verdict
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ui.py tests/test_app.py -q`
Expected: `ImportError: cannot import name 'build_banner'` and `TypeError` on `run()` kwargs.

- [ ] **Step 4: Implement `ui.py`**

Replace `src/harbor_console/ui.py` with:

```python
"""UI rendering for Harbor Console.

Renders only. The one piece of colour on this surface is the platform
banner (ADR 21): red, three rows, above the dashboard, and only while the
verdict says the host's own machinery is broken or the status page has
stopped reporting. Healthy renders exactly what it rendered before the
banner existed, so the three rows of headroom ADR 20 leaves stay free.
"""

from __future__ import annotations

from datetime import datetime

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from harbor_console.checks import STATE_FAILED, is_stale, platform_broken
from harbor_console.gpu import GpuEntry, format_gpu
from harbor_console.storage import StorageEntry, format_entry
from harbor_console.verdict import Verdict

BANNER_ROWS = 3
#: The console is 80 cells wide (ADR 20). Rows are padded to it so the red
#: background is a solid block, and truncated to it so nothing wraps into
#: the headroom.
BANNER_WIDTH = 80
BANNER_STYLE = "bold white on red"


def build_banner(verdict: Verdict | None, now: datetime, missing_since: datetime) -> Text | None:
    """Three rows of red, or None when there is nothing to shout about.

    A missing file is judged against `missing_since` (when this console
    started) the same way a present one is judged against its `written`:
    a web service that has not written for 90 s has gone quiet, and so has
    one that never wrote in the 90 s since the console came up. Within that
    window a missing file is just a page still starting.
    """
    if verdict is None:
        if not is_stale(missing_since, now):
            return None
        lines = [f"status page has not reported since {missing_since:%H:%M:%S}"]
    elif is_stale(verdict.written, now):
        lines = [f"status page has not reported since {verdict.written:%H:%M:%S}"]
    elif platform_broken(verdict.checks):
        failed = [c for c in verdict.checks if c.state == STATE_FAILED]
        lines = [f"{c.name}: {c.reason}" for c in failed[: BANNER_ROWS - 1]]
        if len(failed) > BANNER_ROWS - 1:
            lines[-1] = f"+{len(failed) - (BANNER_ROWS - 2)} more, see the status page"
    else:
        return None

    rows = ["PLATFORM BROKEN", *lines]
    rows += [""] * (BANNER_ROWS - len(rows))
    padded = [row[:BANNER_WIDTH].ljust(BANNER_WIDTH) for row in rows[:BANNER_ROWS]]
    return Text("\n".join(padded), style=BANNER_STYLE, no_wrap=True, overflow="ellipsis")


def build_dashboard(
    metrics: dict[str, str | float | int],
    storage: tuple[StorageEntry, ...] = (),
    gpus: tuple[GpuEntry, ...] = (),
    banner: Text | None = None,
) -> Panel | Group:
    """Build a renderable dashboard panel from collected metrics, storage and GPUs."""
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("Metric", no_wrap=True)
    table.add_column("Value")

    table.add_row("Hostname", str(metrics["hostname"]))
    table.add_row("Uptime", str(metrics["uptime"]))
    table.add_row("CPU utilization", f"{float(metrics['cpu_utilization']):.1f}%")
    table.add_row("Memory", str(metrics["memory_summary"]))
    table.add_row("Swap", str(metrics["swap_summary"]))
    for entry in storage:
        table.add_row(entry.label, format_entry(entry))
    # An empty tuple is a host with no card, and that is a fact worth a row:
    # a blank where the GPU line should be reads as a render bug.
    if gpus:
        for gpu in gpus:
            table.add_row(gpu.label, format_gpu(gpu))
    else:
        table.add_row("GPU", "none detected")
    table.add_row("IPv4 address", str(metrics["ipv4_address"]))
    table.add_row("Docker containers", str(metrics["docker_container_count"]))
    table.add_row("Current date/time", str(metrics["current_datetime"]))

    panel = Panel(table, title="Harbor Console", border_style="white")
    if banner is None:
        return panel
    return Group(banner, panel)
```

Note on the collapse arithmetic: with two content rows, three or more failures show the first failure on row 2 and `+N more` on row 3, where N is the total minus the one shown. `len(failed) - (BANNER_ROWS - 2)` is `len(failed) - 1`. The test with five failures expects `+4 more`.

- [ ] **Step 5: Implement `app.py`**

Replace `src/harbor_console/app.py` with:

```python
"""Application entrypoint and refresh loop."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

from rich.live import Live

from harbor_console.gpu import GpuEntry, collect_gpus
from harbor_console.storage import StorageEntry, collect_storage
from harbor_console.system import collect_system_metrics
from harbor_console.ui import build_banner, build_dashboard
from harbor_console.verdict import Verdict, read_verdict


MetricsCollector = Callable[[], dict[str, str | float | int]]
StorageCollector = Callable[[], tuple[StorageEntry, ...]]
GpuCollector = Callable[[], tuple[GpuEntry, ...]]
VerdictReader = Callable[[], Verdict | None]
DashboardBuilder = Callable[
    [dict[str, str | float | int], tuple[StorageEntry, ...], tuple[GpuEntry, ...], object],
    object,
]


def run(
    refresh_interval: float = 1.0,
    collector: MetricsCollector = collect_system_metrics,
    renderer: DashboardBuilder = build_dashboard,
    sleep: Callable[[float], None] = time.sleep,
    storage_collector: StorageCollector = collect_storage,
    gpu_collector: GpuCollector = collect_gpus,
    verdict_reader: VerdictReader = read_verdict,
    clock: Callable[[], datetime] = datetime.now,
) -> int:
    """Run the Harbor Console refresh loop.

    The verdict is read from a file once per tick and never probed here:
    this loop runs at 1 Hz on tty1 and must never wait on a socket. A
    missing file is judged against when this loop started (`build_banner`).
    """
    started = clock()

    def frame() -> object:
        now = clock()
        banner = build_banner(verdict_reader(), now, started)
        return renderer(collector(), storage_collector(), gpu_collector(), banner)

    try:
        with Live(frame(), refresh_per_second=4, screen=True) as live:
            while True:
                sleep(refresh_interval)
                live.update(frame())
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. A bare invocation runs the tty1 dashboard."""
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ui.py tests/test_app.py -q`
Expected: all pass. If `test_banner_names_the_failed_checks_on_three_rows` fails on line length, the `Text` is wrapping instead of truncating: confirm `no_wrap=True, overflow="ellipsis"` are both set and that the console in the test is `width=80`.

- [ ] **Step 7: Commit**

```bash
git add src/harbor_console/ui.py src/harbor_console/app.py tests/test_ui.py tests/test_app.py
git commit -m "feat: the console shows a three-row red banner while the platform is broken

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: `harbor-console-check`

**Files:**
- Create: `src/harbor_console/check.py`
- Modify: `pyproject.toml`
- Test: `tests/test_check.py`

**Interfaces:**
- Consumes: `read_verdict` (Task 4); `freshness_check`, `platform_broken`, `STATE_*` (Task 1).
- Produces: `check.main(argv=None, reader=read_verdict, clock=datetime.now, out=sys.stdout) -> int`; exit codes `EXIT_OK = 0`, `EXIT_FAILED = 1`, `EXIT_STALE = 2`; console script `harbor-console-check = "harbor_console.check:main"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_check.py`:

```python
import io
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

from harbor_console import check
from harbor_console.checks import STATE_FAILED, STATE_OK, STATE_UNKNOWN, Check
from harbor_console.verdict import Verdict

NOW = datetime(2026, 10, 9, 17, 21, 46)


def run(verdict, now=NOW):
    out = io.StringIO()
    code = check.main(reader=lambda: verdict, clock=lambda: now, out=out)
    return code, out.getvalue()


def test_healthy_exits_zero_and_lists_every_check():
    verdict = Verdict(NOW, "hpz440", (
        Check("docker", STATE_OK, "Docker answered"),
        Check("certificate", STATE_UNKNOWN, "not checked: no tailnet address"),
    ))

    code, text = run(verdict)

    assert code == check.EXIT_OK
    assert "PASS docker\n" in text
    assert "UNKNOWN certificate: not checked: no tailnet address\n" in text
    assert "PASS prober-fresh" in text
    assert text.rstrip().endswith("verdict: PLATFORM OK (2 passed, 1 unknown), written 2026-10-09 17:21:46")


def test_a_failed_check_exits_one():
    verdict = Verdict(NOW, "hpz440", (Check("own-route", STATE_FAILED, "504"),))

    code, text = run(verdict)

    assert code == check.EXIT_FAILED
    assert "FAIL own-route: 504\n" in text
    assert "verdict: PLATFORM BROKEN (1 failed)" in text


def test_a_stale_verdict_exits_two():
    verdict = Verdict(NOW - timedelta(minutes=5), "hpz440", (Check("docker", STATE_OK, "x"),))

    code, text = run(verdict)

    assert code == check.EXIT_STALE
    assert "FAIL prober-fresh: status page has not reported since 17:16:46" in text


def test_a_missing_verdict_exits_two():
    code, text = run(None)

    assert code == check.EXIT_STALE
    assert "no verdict" in text


def test_the_console_script_is_registered():
    pyproject = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())

    assert pyproject["project"]["scripts"]["harbor-console-check"] == "harbor_console.check:main"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_check.py -q`
Expected: `ModuleNotFoundError: No module named 'harbor_console.check'`

- [ ] **Step 3: Write the command and register it**

Create `src/harbor_console/check.py`:

```python
"""`harbor-console-check`: print the verdict file and exit accordingly.

The third surface over the same verdict, for a terminal: `install.sh` ends
with it so a deploy that leaves the platform broken fails where the
operator is looking, and a human can run it over SSH. No flags; it reads
the one file and judges prober-fresh itself, like every other reader.

Exit 0: fresh and nothing failed. 1: something failed. 2: nothing has
reported, or not for 90 s -- a different problem from a failed check, and
one `install.sh` waits out before giving up.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import datetime
from typing import TextIO

from harbor_console.checks import (
    STATE_FAILED,
    STATE_OK,
    STATE_UNKNOWN,
    freshness_check,
    platform_broken,
)
from harbor_console.verdict import Verdict, read_verdict

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_STALE = 2

_LABELS = {STATE_OK: "PASS", STATE_FAILED: "FAIL", STATE_UNKNOWN: "UNKNOWN"}


def main(
    argv: list[str] | None = None,
    reader: Callable[[], Verdict | None] = read_verdict,
    clock: Callable[[], datetime] = datetime.now,
    out: TextIO = sys.stdout,
) -> int:
    verdict = reader()
    now = clock()
    if verdict is None:
        print("verdict: no verdict has been written yet (is harbor-console-web running?)", file=out)
        return EXIT_STALE

    fresh = freshness_check(verdict.written, now)
    checks = verdict.checks + (fresh,)
    for check in checks:
        line = f"{_LABELS.get(check.state, check.state.upper())} {check.name}"
        if check.state != STATE_OK:
            line += f": {check.reason}"
        print(line, file=out)

    passed = sum(1 for c in checks if c.state == STATE_OK)
    failed = sum(1 for c in checks if c.state == STATE_FAILED)
    unknown = sum(1 for c in checks if c.state == STATE_UNKNOWN)
    counts = [f"{passed} passed"]
    if failed:
        counts.insert(0, f"{failed} failed")
    if unknown:
        counts.append(f"{unknown} unknown")
    headline = "PLATFORM BROKEN" if platform_broken(checks) else "PLATFORM OK"
    print(
        f"verdict: {headline} ({', '.join(counts)}), written {verdict.written:%Y-%m-%d %H:%M:%S}",
        file=out,
    )

    if fresh.state == STATE_FAILED:
        return EXIT_STALE
    if failed:
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
```

Note the failed-count line for the broken test: with one failure and zero passes the counts read `1 failed, 0 passed`, and the test only asserts the `(1 failed` prefix.

In `pyproject.toml`, under `[project.scripts]` add:

```toml
harbor-console-check = "harbor_console.check:main"
```

Then run `uv sync --extra dev` so the script is installed into the venv.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_check.py -q`
Expected: `5 passed`

Then: `uv run harbor-console-check; echo "exit=$?"`
Expected on this Windows workstation: `verdict: no verdict has been written yet (is harbor-console-web running?)` and `exit=2`.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/check.py pyproject.toml uv.lock tests/test_check.py
git commit -m "feat: harbor-console-check prints the verdict and exits accordingly

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Units, installer, docs, ADR 21

**Files:**
- Modify: `deploy/harbor-console-web.service`
- Modify: `deploy/install.sh`
- Modify: `tests/test_deploy.py`
- Create: `docs/adr/0021-colour-the-platform-banner-and-nothing-else.md`
- Modify: `docs/deployment.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: `harbor-console-check` exit codes (Task 9).

- [ ] **Step 1: Write the failing deploy tests**

Append to `tests/test_deploy.py`:

```python
WEB_UNIT = REPO / "deploy" / "harbor-console-web.service"
INSTALL = REPO / "deploy" / "install.sh"


def test_the_web_unit_owns_the_verdict_directory():
    text = WEB_UNIT.read_text(encoding="utf-8")

    assert "RuntimeDirectory=harbor-console\n" in text
    assert "RuntimeDirectoryPreserve=yes\n" in text


def test_the_installer_ends_with_the_platform_checks():
    text = INSTALL.read_text(encoding="utf-8")

    assert ".venv/bin/harbor-console-check" in text
    assert text.index("harbor-console-check") > text.index("Bringing up hosted infrastructure")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_deploy.py -q`
Expected: 2 failed.

- [ ] **Step 3: The web unit**

In `deploy/harbor-console-web.service`, after the `PrivateTmp=yes` line add:

```ini
# /run/harbor-console, owned by harbor: where the prober writes the
# verdict file the tty1 console and harbor-console-check read. Preserved
# across restarts so a deploy does not blank the console's banner for the
# first cycle; staleness (90 s) is what retires an old verdict, not the
# service stopping.
RuntimeDirectory=harbor-console
RuntimeDirectoryPreserve=yes
```

- [ ] **Step 4: The installer**

In `deploy/install.sh`, after the `docker compose -p hosted up -d --remove-orphans` line and before the `echo` / "Harbor Console is installed" block, insert:

```bash
# The deploy's own acceptance check. The prober's first cycle after the
# restart above takes up to 30 s, and Traefik's file route to the page has
# to reload before the own-route probe can pass, so wait up to a minute
# for a clean verdict rather than judging the first second. Exit code 2
# (nothing written yet, or stale) is what the wait is for; 1 (a check
# failed) is a real verdict and ends the wait early.
echo "==> Waiting for the platform checks"
check_status=2
check_output=""
for _ in $(seq 1 12); do
  check_output=$("${INSTALL_DIR}/.venv/bin/harbor-console-check" 2>&1) && check_status=0 || check_status=$?
  if [[ ${check_status} -ne 2 ]]; then
    break
  fi
  sleep 5
done
```

Then replace the final `for unit ... systemctl status ... done` loop at the end of the script with:

```bash
for unit in "${UNIT_NAMES[@]}"; do
  systemctl status "${unit}" --no-pager || true
done
echo
echo "${check_output}"
if [[ ${check_status} -ne 0 ]]; then
  echo "Deploy finished, but the platform checks did not pass (see above). The status page and tty1 show the same banner until they do." >&2
  exit "${check_status}"
fi
```

- [ ] **Step 5: ADR 21**

Create `docs/adr/0021-colour-the-platform-banner-and-nothing-else.md`:

```markdown
# 21. Colour the platform banner, and nothing else

Date: 2026-10-09

## Status

Accepted

## Context

The founding document deferred colour: "No colors are required." Both
surfaces have been monochrome since, and the tty1 dashboard is read from
across a room, where a palette would be noise.

Three times now the edge has broken while every process stayed up and
nothing shouted. ADR 17: Traefik's default route landed on the wrong
network and every proxied request to the page timed out. ADR 19: a reboot
moved Traefik off the address the firewall rule named, same symptom,
different cause. On 2026-10-08 an `apt upgrade` to Docker Engine 29 refused
the API version Traefik v3.3 pinned; Traefik ran, the certificate stayed
valid, and every label-declared route answered 404 for 23 hours. In two of
the three the evidence was already on the status page as `DOWN` rows.
Nobody was looking, and the page did nothing to make them.

The earlier scope expansions (ADR 6, ADR 15) each had a failure that had
already happened as their bar. This one has three.

## Decision

We will judge the host's own machinery from what the page already collects
-- Docker, Traefik and its Docker provider, the page's own route through
the proxy, the served certificate, the edge's listeners, the prober's own
freshness -- and while any of those fails, both surfaces take over their
top with a red banner that says so: a block above everything on the status
page, three rows above the dashboard on tty1.

The banner is platform only. A declared service being down is a row-level
`DOWN`, as before. A check whose evidence is missing is `unknown` and never
trips the banner (ADR 18's rule, applied to judgement).

The verdict crosses from the web prober to the console as a file,
`/run/harbor-console/checks.json`, written atomically every cycle and read
once per 1 Hz tick. The console never probes. `harbor-console-check` reads
the same file so `install.sh` can end by failing loudly.

Red is the only colour either surface uses. It means one thing: the host
is broken. Nothing else -- not state cells, not findings, not GPU
temperatures -- gets a colour, so the banner cannot be confused with
decoration and its absence stays meaningful.

## Consequences

- An edge outage shows within 90 s on the monitor in the room and on the
  page, instead of in a log a day later.
- A deploy that leaves the platform broken exits non-zero in the terminal.
- A dev container down for its own reasons does not turn the host red. The
  cost is that a single broken service is still only a `DOWN` row; a
  second tier is explicitly deferred.
- The founding document's "no colours" narrows to "no colours but this one".
  Adding a second colour anywhere needs a new ADR.
- The page gains one probe of itself through the proxy and one TLS
  handshake per cycle, both with 2 s bounds, inside the existing prober
  thread. The console gains one small file read per tick.
- Rejected: a `/checks` endpoint pulled by the console (needs the
  one-endpoint rule retired and a thread in the console); a third timer
  unit (re-runs every collector); notifications (still deferred).
```

- [ ] **Step 6: Docs**

In `docs/deployment.md`, under "The edge (Traefik)" or wherever the install's closing output is described (search for "Harbor Console is installed"), add a short subsection:

```markdown
### The platform checks

`install.sh` ends by waiting up to a minute for `harbor-console-check` to
report a clean verdict, prints its output, and exits non-zero if the
platform is broken. The same verdict drives the red banner on the status
page and on tty1 (ADR 21). The checks are platform only: Docker, Traefik and
its Docker provider, the page's own route through the proxy, the certificate
(fails under 14 days left), the edge's tailnet listeners, and whether the
prober has reported in the last 90 s. A declared service being down stays a
`DOWN` row and never trips the banner.

The verdict lives at `/run/harbor-console/checks.json`, written by
`harbor-console-web` every 30 s. Run `harbor-console-check` from
`/opt/harbor-console/.venv/bin/` over SSH to read it; exit 0 is healthy, 1
is a failed check, 2 is nothing reported (is the web service running?).
```

And in the troubleshooting list after the "Every label-declared route 404s" bullet, add:

```markdown
- The banner says `PLATFORM BROKEN` on tty1 but the status page looks
  healthy: the console reads `/run/harbor-console/checks.json`; if the
  banner line is `status page has not reported since ...`, the web unit is
  up but not writing. `journalctl -u harbor-console-web` shows a
  `could not write /run/harbor-console/checks.json` line if the directory
  is missing, which an `install.sh` re-run fixes by reinstalling the unit.
```

In `CLAUDE.md`, in the "Architecture" section's web-surface list, add after the `snapshot.py` bullet:

```markdown
- `checks.py` — the third policy, pure: one cycle's evidence in, six named platform checks out (`docker`, `traefik-api`, `docker-provider`, `own-route`, `certificate`, `edge-listening`), each `ok`, `failed` or `unknown` with a reason. Platform only; a declared service being down is a `DOWN` row, never a check. `unknown` never trips the banner. `freshness_check` is the seventh, `prober-fresh`, judged by whoever reads the verdict because a writer cannot report its own silence ([ADR 21](docs/adr/0021-colour-the-platform-banner-and-nothing-else.md)).
- `certificate.py` — **collects** the certificate the edge serves on the tailnet address: one stdlib `ssl` handshake with the page's own route name as SNI, 2 s bound, returning names and expiry or `CertificateUnavailable` with the reason. A failed handshake is a failed check, not missing evidence.
- `verdict.py` — the **contract** that crosses processes: `Verdict` as JSON at `/run/harbor-console/checks.json`, written atomically by the web prober every cycle, read by the console once per tick and by `harbor-console-check`. `loads` and `read_verdict` never raise.
- `check.py` — `harbor-console-check`: prints the verdict and exits 0 (healthy), 1 (a check failed) or 2 (nothing reported or stale). `install.sh` ends with it.
```

Update the `ui.py` and `app.py` bullets in the same section: `ui.py` also renders the 3-row red banner from a `Verdict` (`build_banner`), the only colour on the surface; `app.py` reads the verdict file once per tick through an injectable `verdict_reader` and never probes. Add a fifth bullet to the "Four behaviours of that service are load-bearing" list:

```markdown
- **The verdict file is the only thing the console reads from the web service, and the console never probes.** The 1 Hz tty1 loop reads `/run/harbor-console/checks.json` and nothing else new; a missing or stale file is itself the banner ("status page has not reported"). Moving the console onto a socket, or giving it a collector that can block, undoes ADR 4's "never crashes, never stalls" ([ADR 21](docs/adr/0021-colour-the-platform-banner-and-nothing-else.md)).
```

In the "Scope discipline" paragraph that says "There are intentionally no colors", amend to: "There are intentionally no colors except the red platform banner (ADR 21), no keyboard shortcuts, ...".

- [ ] **Step 7: Run everything**

Run: `uv run pytest -q`
Expected: all pass, roughly 430 tests.

Run: `bash -n deploy/install.sh`
Expected: no output (the script parses).

- [ ] **Step 8: Commit**

```bash
git add deploy/harbor-console-web.service deploy/install.sh tests/test_deploy.py docs/adr/0021-colour-the-platform-banner-and-nothing-else.md docs/deployment.md CLAUDE.md
git commit -m "deploy: the web unit owns /run/harbor-console and install.sh ends with the platform checks

ADR 21 records the banner, the platform-only rule and the file contract.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Deploy and verify on hpz440

**Files:** none. This task is operational.

- [ ] **Step 1: Push and open the PR**

```bash
git push -u origin feat/platform-checks
gh pr create --title "feat: platform checks and the broken banner" --body-file - <<'EOF'
## Summary

Judges the host's own machinery every prober cycle and takes over the top of the status page and tty1 with a red banner while anything fails. Spec: `docs/superpowers/specs/2026-10-09-platform-checks-design.md`. ADR 21.

- `checks.py`: docker, traefik-api, docker-provider (the 2026-10-08 outage), own-route (ADR 17/19), certificate (<14 days or no wildcard), edge-listening; plus prober-fresh judged by each reader.
- `certificate.py`: one stdlib TLS handshake to the tailnet address.
- `verdict.py`: `/run/harbor-console/checks.json`, atomic, read by the console once per tick.
- `harbor-console-check`: exit 0/1/2; `install.sh` ends by waiting up to 60 s for it.
- Platform only; `unknown` never trips the banner; red is the only colour.

## Test plan

- [x] `uv run pytest` passes
- [ ] Deployed to hpz440: `install.sh` ends with `verdict: PLATFORM OK`
- [ ] `docker stop traefik` for two minutes turns tty1 and the page red; `docker start traefik` clears them within 90 s

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
```

- [ ] **Step 2: Merge and deploy**

Conrad merges (`gh pr merge <n> --merge --delete-branch`) and runs, interactively because sudo needs a password:

```
ssh -t conrad@hpz440 'git -C /srv/harbor-console pull && sudo bash /srv/harbor-console/deploy/install.sh'
```

Expected: the script ends with the check output and `verdict: PLATFORM OK (6 passed), written ...` or `PLATFORM OK (5 passed, 1 unknown)` and exit 0.

- [ ] **Step 3: Verify from the gte side**

```bash
ssh gte@hpz440 '/opt/harbor-console/.venv/bin/harbor-console-check; echo exit=$?; cat /run/harbor-console/checks.json; curl -s http://100.69.239.123:8100/ | grep -o "Platform checks:[^<]*"'
```

Expected: every line `PASS`, exit 0, the JSON file present, and the page footer counting 7 passed.

- [ ] **Step 4: Prove the banner**

Conrad runs `ssh conrad@hpz440 'docker stop traefik'`, waits two minutes, and checks tty1 (red banner: `traefik-api`, `own-route`, `edge-listening`, `certificate`) and the page at `http://100.69.239.123:8100/` (red block). Then `docker start traefik`; both clear within 90 s. Tick the PR's test plan boxes.
