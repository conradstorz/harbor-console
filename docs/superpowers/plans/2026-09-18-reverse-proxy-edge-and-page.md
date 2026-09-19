# Reverse Proxy: Edge and Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put Traefik in front of every HTTP service on hpz440 and make the harbor-console page a directory of what Traefik routes and what compose labels declare, retiring the port ledger and allocator.

**Architecture:** Traefik runs from `deploy/traefik/compose.yaml`, bound to the tailnet address only, with Cloudflare DNS-01 for one wildcard cert. The page keeps the collect / render / coordinate split: `docker.py` reads labels via `docker inspect`, new `traefik.py` reads the routers API on loopback, pure `directory.py` turns containers + routers + listeners into rows and findings, `web.py` renders, `webapp.py` coordinates. Everything under `ports/`, plus `serve.py`, `addressing.py`, `reconcile.py`, `services.toml` and `.harbor.toml`, is deleted.

**Tech Stack:** Python 3.13, stdlib `http.server`/`urllib`/`json`, `psutil`, `rich` (tty dashboard, untouched), pytest via `uv`. Traefik v3 container. No new Python dependency.

**Spec:** `docs/superpowers/specs/2026-09-18-reverse-proxy-design.md`

## Global Constraints

- Nothing binds `0.0.0.0`. Traefik publishes `100.69.239.123:80` and `:443`; the page binds the tailnet address on `8100`; Traefik's API is `127.0.0.1:8081`.
- Stdlib only for the page. No new runtime dependency in `pyproject.toml`.
- Collectors degrade, never raise, except `tailnet.py`. `DOCKER_UNAVAILABLE` and `TRAEFIK_UNAVAILABLE` distinguish "could not ask" from "asked, nothing there".
- Probing happens in the background thread only. Any HTTP response means up.
- The page is read-only.
- Route names: `<name>.hpz440.ohr3023.org`. Route host strings come from labels and Traefik, never from a constant in Python.
- Label convention: HTTP = `traefik.enable=true` + `traefik.http.routers.<name>.rule=Host(...)`; TCP = `harbor.kind=tcp` + `harbor.port=<n>`; internal = `harbor.kind=internal`; edge = `harbor.kind=edge` (Traefik only). Optional `harbor.description`.
- Tests: fakes injected, no real sockets, no real time, no real Docker.
- Every task leaves `uv run pytest` green. Run tests as `uv run pytest`; never chain commands with `&&`.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File map

| Path | Responsibility | Fate |
|---|---|---|
| `src/harbor_console/docker.py` | containers with labels, published ports, networks | rewrite |
| `src/harbor_console/traefik.py` | routers from Traefik's API | create |
| `src/harbor_console/listening.py` | listening sockets; gains `addrs_overlap` | modify |
| `src/harbor_console/directory.py` | pure policy: rows and findings | create |
| `src/harbor_console/probe.py` | liveness by URL | modify signature |
| `src/harbor_console/snapshot.py` | prober → renderer contract | rewrite fields |
| `src/harbor_console/web.py` | render page, serve `/` | rewrite tables, drop `/ports.json` |
| `src/harbor_console/webapp.py` | entry point, prober thread | rewrite without ledger |
| `src/harbor_console/ports/**`, `serve.py`, `addressing.py`, `reconcile.py` | ledger era | delete |
| `services.toml`, `.harbor.toml`, `HARBOR_PORTS.md` | ledger era | delete |
| `deploy/traefik/compose.yaml`, `deploy/traefik/dynamic/harbor.yml.in` | the edge | create |
| `deploy/install.sh`, `deploy/harbor-console-web.service` | deploy | modify |
| `docs/adr/0015-reverse-proxy-and-label-declared-services.md` | decision | create |
| `CLAUDE.md`, `README.md`, `docs/deployment.md`, `docs/architecture.md` | docs | modify |

---

### Task 1: `docker.py` reads labels and networks via `docker inspect`

**Files:**
- Modify: `src/harbor_console/docker.py`
- Test: `tests/test_docker.py`

**Interfaces:**
- Produces: `Container(name: str, published: tuple[tuple[str, int], ...], labels: dict[str, str] = {}, networks: frozenset[str] = frozenset())`, frozen dataclass. `running_containers(run=subprocess.run, timeout=DOCKER_TIMEOUT_SECONDS) -> tuple[Container, ...] | DOCKER_UNAVAILABLE`. `DOCKER_UNAVAILABLE` unchanged.
- Existing callers (`reconcile.py`, `web.py`, tests) construct `Container(name, published)` positionally; the new fields default so they keep working until deleted in Task 9.

- [ ] **Step 1: Replace the tests in `tests/test_docker.py`**

```python
import json
import subprocess
from types import SimpleNamespace

from harbor_console.docker import DOCKER_UNAVAILABLE, Container, running_containers


def inspect_entry(name, ports=None, labels=None, networks=("bridge",)):
    return {
        "Name": f"/{name}",
        "Config": {"Labels": labels or {}},
        "NetworkSettings": {
            "Ports": ports or {},
            "Networks": {net: {} for net in networks},
        },
    }


def fake_run(ids="", inspected=(), returncode=0, raises=None):
    """`docker ps -q` answers `ids`; `docker inspect` answers `inspected`."""
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if raises is not None:
            raise raises
        if args[:2] == ["docker", "ps"]:
            return SimpleNamespace(stdout=ids, returncode=returncode)
        return SimpleNamespace(stdout=json.dumps(list(inspected)), returncode=returncode)

    run.calls = calls
    return run


def test_parses_name_labels_ports_and_networks():
    entry = inspect_entry(
        "parksmart-parksmart-1",
        ports={"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]},
        labels={"traefik.enable": "true", "harbor.description": "parking"},
        networks=("harbor", "parksmart_default"),
    )

    result = running_containers(run=fake_run("abc\n", [entry]))

    assert result == (
        Container(
            "parksmart-parksmart-1",
            (("127.0.0.1", 8000),),
            {"traefik.enable": "true", "harbor.description": "parking"},
            frozenset({"harbor", "parksmart_default"}),
        ),
    )


def test_ipv6_wildcard_publish_is_normalised_and_deduplicated():
    entry = inspect_entry(
        "web",
        ports={
            "8080/tcp": [
                {"HostIp": "0.0.0.0", "HostPort": "8080"},
                {"HostIp": "::", "HostPort": "8080"},
            ]
        },
    )

    result = running_containers(run=fake_run("a\n", [entry]))

    assert result[0].published == (("0.0.0.0", 8080),)


def test_exposed_but_unpublished_ports_are_ignored():
    entry = inspect_entry("db", ports={"5432/tcp": None})

    result = running_containers(run=fake_run("a\n", [entry]))

    assert result[0].published == ()
    assert result[0].labels == {}


def test_udp_publishes_are_ignored():
    entry = inspect_entry("dns", ports={"53/udp": [{"HostIp": "0.0.0.0", "HostPort": "53"}]})

    assert running_containers(run=fake_run("a\n", [entry]))[0].published == ()


def test_containers_sort_by_name():
    entries = [inspect_entry("zeta"), inspect_entry("alpha")]

    result = running_containers(run=fake_run("a\nb\n", entries))

    assert [c.name for c in result] == ["alpha", "zeta"]


def test_no_running_containers_skips_inspect():
    run = fake_run("")

    assert running_containers(run=run) == ()
    assert len(run.calls) == 1


def test_missing_binary_reports_unavailable():
    assert running_containers(run=fake_run(raises=FileNotFoundError())) is DOCKER_UNAVAILABLE


def test_nonzero_exit_reports_unavailable():
    assert running_containers(run=fake_run("a\n", returncode=1)) is DOCKER_UNAVAILABLE


def test_a_hanging_daemon_reports_unavailable_rather_than_blocking_forever():
    raises = subprocess.TimeoutExpired(cmd=["docker"], timeout=2.0)

    assert running_containers(run=fake_run(raises=raises)) is DOCKER_UNAVAILABLE


def test_malformed_inspect_json_reports_unavailable():
    def run(args, **_kwargs):
        if args[:2] == ["docker", "ps"]:
            return SimpleNamespace(stdout="a\n", returncode=0)
        return SimpleNamespace(stdout="not json", returncode=0)

    assert running_containers(run=run) is DOCKER_UNAVAILABLE


def test_one_malformed_entry_is_skipped_not_fatal():
    entries = [{"Name": "/ok"}, inspect_entry("fine")]

    result = running_containers(run=fake_run("a\nb\n", entries))

    assert [c.name for c in result] == ["fine", "ok"]
    assert result[1].published == ()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_docker.py -v`
Expected: FAIL — `Container.__init__() takes from 3 to 3 positional arguments` and unpacking errors.

- [ ] **Step 3: Rewrite `src/harbor_console/docker.py`**

```python
"""Live container state: names, labels, published ports, networks.

Labels are how a container declares itself (see `directory.py`): Traefik's
`traefik.*` labels for an HTTP route, `harbor.kind` for everything else. The
page can only report an undeclared container if it can read labels, which is
why this collector uses `docker inspect` rather than `docker ps --format`.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field


class _Unavailable(tuple):
    """A distinguishable empty result: falsy, iterable, and identity-checkable."""


#: Returned when Docker could not be asked at all, so a caller can tell that
#: apart from "asked, and nothing is running" -- the difference decides whether
#: the page may claim a container is undeclared.
DOCKER_UNAVAILABLE = _Unavailable()

#: A bound on each docker call. This runs inside the prober thread on every
#: cycle; a wedged daemon must not freeze the last good snapshot in place.
DOCKER_TIMEOUT_SECONDS = 2.0

IPV6_ANY = "::"
IPV4_ANY = "0.0.0.0"


@dataclass(frozen=True)
class Container:
    """One running container: what it publishes and what it declares."""

    name: str
    published: tuple[tuple[str, int], ...]
    labels: dict[str, str] = field(default_factory=dict)
    networks: frozenset[str] = frozenset()


def running_containers(
    run: Callable[..., object] = subprocess.run,
    timeout: float = DOCKER_TIMEOUT_SECONDS,
) -> tuple[Container, ...]:
    """Collect running containers. Returns DOCKER_UNAVAILABLE if Docker cannot be read.

    Two calls: `docker ps -q` for the running set, then one `docker inspect`
    over all of it. Either failing, hanging, or answering something that is
    not JSON is the same outage. One malformed entry inside good JSON is
    skipped, the way `listening.py` skips one bad socket: the rest is still
    evidence.
    """
    try:
        listed = run(
            ["docker", "ps", "-q"],
            check=False, capture_output=True, text=True, timeout=timeout,
        )
        if listed.returncode != 0:  # type: ignore[attr-defined]
            return DOCKER_UNAVAILABLE
        ids = listed.stdout.split()  # type: ignore[attr-defined]
        if not ids:
            return ()
        inspected = run(
            ["docker", "inspect", *ids],
            check=False, capture_output=True, text=True, timeout=timeout,
        )
        if inspected.returncode != 0:  # type: ignore[attr-defined]
            return DOCKER_UNAVAILABLE
        entries = json.loads(inspected.stdout)  # type: ignore[attr-defined]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return DOCKER_UNAVAILABLE

    if not isinstance(entries, list):
        return DOCKER_UNAVAILABLE

    containers = []
    for entry in entries:
        container = _parse(entry)
        if container is not None:
            containers.append(container)
    return tuple(sorted(containers, key=lambda item: item.name))


def _parse(entry: object) -> Container | None:
    """One inspect entry to one Container; None when it has no usable name."""
    if not isinstance(entry, dict):
        return None
    name = entry.get("Name")
    if not isinstance(name, str) or not name:
        return None
    config = entry.get("Config") if isinstance(entry.get("Config"), dict) else {}
    labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
    settings = (
        entry.get("NetworkSettings")
        if isinstance(entry.get("NetworkSettings"), dict)
        else {}
    )
    ports = settings.get("Ports") if isinstance(settings.get("Ports"), dict) else {}
    networks = settings.get("Networks") if isinstance(settings.get("Networks"), dict) else {}
    return Container(
        name=name.lstrip("/"),
        published=_publish_pairs(ports),
        labels={str(k): str(v) for k, v in labels.items()},
        networks=frozenset(str(n) for n in networks),
    )


def _publish_pairs(ports: dict) -> tuple[tuple[str, int], ...]:
    """Host `(addr, port)` pairs from inspect's `NetworkSettings.Ports`."""
    pairs: set[tuple[str, int]] = set()
    for key, bindings in ports.items():
        if not str(key).endswith("/tcp") or not isinstance(bindings, list):
            continue
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            addr = str(binding.get("HostIp", ""))
            addr = IPV4_ANY if addr in ("", IPV6_ANY) else addr
            try:
                pairs.add((addr, int(binding.get("HostPort"))))
            except (TypeError, ValueError):
                continue
    return tuple(sorted(pairs))
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest`
Expected: all pass. `test_reconcile.py`, `test_web.py`, `test_webapp.py` build `Container(name, published)` positionally and still work.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/docker.py tests/test_docker.py
git commit -m "feat(docker): read labels and networks via docker inspect

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `traefik.py` collects routers from the API

**Files:**
- Create: `src/harbor_console/traefik.py`
- Test: `tests/test_traefik.py`

**Interfaces:**
- Produces: `Router(name: str, host: str | None, service: str, enabled: bool, error: str | None)`, frozen. `TRAEFIK_UNAVAILABLE` sentinel (same `_Unavailable` shape as Docker's). `traefik_routers(opener=urllib.request.urlopen, base=TRAEFIK_API, timeout=TRAEFIK_TIMEOUT_SECONDS) -> tuple[Router, ...]`. `TRAEFIK_API = "http://127.0.0.1:8081"`. `router_name(label_name: str) -> str` returns `f"{label_name}@docker"`.
- `name` is Traefik's full name, e.g. `parksmart@docker`; `host` is parsed from a `Host(`...`)` rule, None when the rule has no Host.

- [ ] **Step 1: Write `tests/test_traefik.py`**

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_traefik.py -v`
Expected: FAIL — `ModuleNotFoundError: harbor_console.traefik`.

- [ ] **Step 3: Write `src/harbor_console/traefik.py`**

```python
"""What Traefik routes, from its own API.

Traefik is the only truth for which hostnames reach which containers, so the
page asks it rather than re-deriving routes from labels. The API is bound to
loopback by `deploy/traefik/compose.yaml`; nothing here is reachable from the
tailnet. Degrades to `TRAEFIK_UNAVAILABLE` on any failure, which the page
reports as absence of knowledge, never as "nothing is routed".
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass


class _Unavailable(tuple):
    """A distinguishable empty result: falsy, iterable, and identity-checkable."""


TRAEFIK_UNAVAILABLE = _Unavailable()
TRAEFIK_API = "http://127.0.0.1:8081"
TRAEFIK_TIMEOUT_SECONDS = 2.0

_HOST = re.compile(r"Host\(`([^`]+)`\)")


@dataclass(frozen=True)
class Router:
    """One Traefik router: a hostname, the service behind it, and Traefik's verdict."""

    name: str
    host: str | None
    service: str
    enabled: bool
    error: str | None


def router_name(label_name: str) -> str:
    """The full Traefik name of a router declared in Docker labels."""
    return f"{label_name}@docker"


def traefik_routers(
    opener: Callable[..., object] = urllib.request.urlopen,
    base: str = TRAEFIK_API,
    timeout: float = TRAEFIK_TIMEOUT_SECONDS,
) -> tuple[Router, ...]:
    """Collect every HTTP router Traefik knows, or TRAEFIK_UNAVAILABLE."""
    try:
        with opener(f"{base}/api/http/routers", timeout=timeout) as response:  # type: ignore[union-attr]
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException, urllib.error.URLError):
        return TRAEFIK_UNAVAILABLE

    if not isinstance(payload, list):
        return TRAEFIK_UNAVAILABLE

    routers = []
    for entry in payload:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        rule = str(entry.get("rule", ""))
        match = _HOST.search(rule)
        errors = entry.get("error")
        error = None
        if isinstance(errors, list) and errors:
            error = "; ".join(str(item) for item in errors)
        elif isinstance(errors, str) and errors:
            error = errors
        routers.append(
            Router(
                name=entry["name"],
                host=match.group(1) if match else None,
                service=str(entry.get("service", "")),
                enabled=entry.get("status") == "enabled",
                error=error,
            )
        )
    return tuple(sorted(routers, key=lambda item: item.name))
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_traefik.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/traefik.py tests/test_traefik.py
git commit -m "feat(traefik): collect routers from the loopback API

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `addrs_overlap` moves to `listening.py`

**Files:**
- Modify: `src/harbor_console/listening.py`
- Modify: `src/harbor_console/ports/keys.py` (re-export until deleted)
- Test: `tests/test_listening.py`

**Interfaces:**
- Produces: `listening.addrs_overlap(a: str, b: str) -> bool`, `listening.ANY_ADDR = "0.0.0.0"`.

- [ ] **Step 1: Add to `tests/test_listening.py`**

```python
from harbor_console.listening import ANY_ADDR, addrs_overlap


def test_wildcard_overlaps_everything():
    assert addrs_overlap(ANY_ADDR, "127.0.0.1")
    assert addrs_overlap("100.69.239.123", ANY_ADDR)


def test_same_specific_address_overlaps():
    assert addrs_overlap("127.0.0.1", "127.0.0.1")


def test_different_specific_addresses_do_not_overlap():
    assert not addrs_overlap("127.0.0.1", "100.69.239.123")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_listening.py -v`
Expected: FAIL — `ImportError: cannot import name 'ANY_ADDR'`.

- [ ] **Step 3: Add to `src/harbor_console/listening.py`** after `IPV4_ANY`:

```python
#: The address that contends with every other on its host.
ANY_ADDR = IPV4_ANY


def addrs_overlap(a: str, b: str) -> bool:
    """True when two bind addresses contend for the same port.

    `0.0.0.0` claims every address on the host, so it overlaps anything. Two
    different specific addresses each hold the same port number without
    conflict -- which is how ARM held 100.69.239.123:49152 without claiming
    49152 from loopback.
    """
    if a == b:
        return True
    return ANY_ADDR in (a, b)
```

In `src/harbor_console/ports/keys.py`, replace the body of `addrs_overlap` and the `ANY_ADDR` definition with imports so there is one definition:

```python
from harbor_console.listening import ANY_ADDR, addrs_overlap  # noqa: F401 - re-exported until ports/ is deleted
```

(Delete the local `ANY_ADDR = ...` line and the local `def addrs_overlap` in `keys.py`.)

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/listening.py src/harbor_console/ports/keys.py tests/test_listening.py
git commit -m "refactor: addrs_overlap lives with the socket collector

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `probe.py` takes a URL

**Files:**
- Modify: `src/harbor_console/probe.py`
- Test: `tests/test_probe.py`

**Interfaces:**
- Produces: `probe(base_url: str, opener=urllib.request.urlopen, timeout=2.0) -> Health`. `base_url` has no trailing slash, e.g. `https://parksmart.hpz440.ohr3023.org`. `Health` and `Detail` unchanged.
- Drops the `harbor_console.addressing` import.

- [ ] **Step 1: Update `tests/test_probe.py`**

Every call of the form `probe("host", 8080, opener=...)` becomes `probe("http://host:8080", opener=...)`. Add one test:

```python
def test_probe_builds_urls_from_the_base_url():
    seen = []

    def opener(url, timeout=None):
        seen.append(url)
        return FakeResponse(b"{}")

    probe("https://parksmart.hpz440.ohr3023.org", opener=opener)

    assert seen == [
        "https://parksmart.hpz440.ohr3023.org/",
        "https://parksmart.hpz440.ohr3023.org/hcstatus",
    ]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_probe.py -v`
Expected: FAIL — `TypeError` from the changed positional arguments.

- [ ] **Step 3: Change `probe` in `src/harbor_console/probe.py`**

Remove `from harbor_console.addressing import url_host`. Replace the signature and first line:

```python
def probe(
    base_url: str,
    opener: Callable[..., object] = urllib.request.urlopen,
    timeout: float = 2.0,
) -> Health:
    """Probe one service for liveness, then for optional detail.

    `base_url` is the route the page prints, so a green row proves the whole
    path through the proxy, TLS included.
    """
    base = base_url.rstrip("/")
```

The rest of the function is unchanged.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest`
Expected: `tests/test_probe.py` passes; `tests/test_webapp.py` fails where `collect_snapshot` calls `prober(host, port)`. Fix by editing `webapp.collect_snapshot`'s health dict to:

```python
    health = {
        (lease.project, lease.name, lease.host): prober(
            f"http://{probe_target(lease, host, tailnet_address)}:{lease.port}"
        )
        for lease in held
    }
```

and in `tests/test_webapp.py` change every `prober=lambda host, port:` to `prober=lambda url:`. Re-run `uv run pytest`; expected all pass. (Task 8 replaces this code entirely; this keeps main green in between.)

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/probe.py src/harbor_console/webapp.py tests/test_probe.py tests/test_webapp.py
git commit -m "refactor(probe): probe a URL, not a host and port

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: `directory.py` — declared kinds and rows

**Files:**
- Create: `src/harbor_console/directory.py`
- Test: `tests/test_directory.py`

**Interfaces:**
- Produces:
  - constants `KIND_HTTP = "http"`, `KIND_TCP = "tcp"`, `KIND_INTERNAL = "internal"`, `KIND_EDGE = "edge"`, `KINDS = frozenset({...})`.
  - `LABEL_ENABLE = "traefik.enable"`, `LABEL_KIND = "harbor.kind"`, `LABEL_PORT = "harbor.port"`, `LABEL_DESCRIPTION = "harbor.description"`.
  - `declared_kind(container: Container) -> str | None`.
  - `route_of(container: Container) -> tuple[str, str | None] | None` → `(router label name, host)` from the first `traefik.http.routers.<name>.rule` label; `(container.name, None)` when `traefik.enable=true` but no rule label; `None` when not HTTP.
  - `Row(name: str, kind: str, target: str, container: str, description: str, state: str)`; `State` strings: `UP`, `DOWN`, `ROUTE ERROR`, `LISTENING`, `INTERNAL`, `UNKNOWN`.
  - `build_rows(containers, routers, listeners, health: Mapping[str, Health], probed: bool) -> tuple[Row, ...]`. `health` is keyed by `Row.name`. `routers` may be `TRAEFIK_UNAVAILABLE`.
- `Finding` comes in Task 6; this task only builds rows.

- [ ] **Step 1: Write `tests/test_directory.py`**

```python
from harbor_console.directory import (
    KIND_EDGE,
    KIND_HTTP,
    KIND_INTERNAL,
    KIND_TCP,
    Row,
    build_rows,
    declared_kind,
    route_of,
)
from harbor_console.docker import Container
from harbor_console.listening import Listener
from harbor_console.probe import Health
from harbor_console.traefik import TRAEFIK_UNAVAILABLE, Router

HOST = "parksmart.hpz440.ohr3023.org"
UP = Health(True, "ok", "3 queued", (), None)
DOWN = Health(False, None, None, (), None)


def http_container(name="parksmart-parksmart-1", route="parksmart", host=HOST, **labels):
    return Container(
        name,
        (),
        {
            "traefik.enable": "true",
            f"traefik.http.routers.{route}.rule": f"Host(`{host}`)",
            **labels,
        },
        frozenset({"harbor"}),
    )


def test_declared_kind_reads_traefik_then_harbor_labels():
    assert declared_kind(http_container()) == KIND_HTTP
    assert declared_kind(Container("mqtt", (), {"harbor.kind": "tcp", "harbor.port": "1883"})) == KIND_TCP
    assert declared_kind(Container("db", (), {"harbor.kind": "internal"})) == KIND_INTERNAL
    assert declared_kind(Container("traefik", (), {"harbor.kind": "edge"})) == KIND_EDGE


def test_an_unknown_kind_is_undeclared():
    assert declared_kind(Container("x", (), {"harbor.kind": "banana"})) is None
    assert declared_kind(Container("x", ())) is None


def test_traefik_enable_false_is_not_http():
    assert declared_kind(Container("x", (), {"traefik.enable": "false"})) is None


def test_route_of_reads_the_router_label():
    assert route_of(http_container()) == ("parksmart", HOST)


def test_route_of_without_a_rule_falls_back_to_the_container_name():
    assert route_of(Container("plain", (), {"traefik.enable": "true"})) == ("plain", None)


def test_route_of_a_non_http_container_is_none():
    assert route_of(Container("db", (), {"harbor.kind": "internal"})) is None


def test_http_row_is_up_when_the_probe_answered():
    routers = (Router("parksmart@docker", HOST, "parksmart", True, None),)

    rows = build_rows((http_container(),), routers, (), {"parksmart": UP}, probed=True)

    assert rows == (
        Row("parksmart", KIND_HTTP, f"https://{HOST}/", "parksmart-parksmart-1", "", "UP"),
    )


def test_http_row_is_down_when_the_probe_failed():
    routers = (Router("parksmart@docker", HOST, "parksmart", True, None),)

    rows = build_rows((http_container(),), routers, (), {"parksmart": DOWN}, probed=True)

    assert rows[0].state == "DOWN"


def test_http_row_is_route_error_when_traefik_disabled_it():
    routers = (Router("parksmart@docker", HOST, "parksmart", False, "no service"),)

    rows = build_rows((http_container(),), routers, (), {"parksmart": DOWN}, probed=True)

    assert rows[0].state == "ROUTE ERROR"


def test_http_row_without_a_rule_is_route_error():
    rows = build_rows((Container("plain", (), {"traefik.enable": "true"}),), (), (), {}, probed=True)

    assert rows[0].state == "ROUTE ERROR"
    assert rows[0].target == ""


def test_http_row_is_unknown_before_the_first_probe():
    rows = build_rows((http_container(),), (), (), {}, probed=False)

    assert rows[0].state == "UNKNOWN"


def test_http_row_is_down_not_route_error_when_traefik_is_unavailable():
    rows = build_rows((http_container(),), TRAEFIK_UNAVAILABLE, (), {"parksmart": DOWN}, probed=True)

    assert rows[0].state == "DOWN"


def test_tcp_row_is_listening_when_the_port_is_held():
    mqtt = Container("ice-colder-mqtt", (("0.0.0.0", 1883),), {"harbor.kind": "tcp", "harbor.port": "1883"})

    rows = build_rows((mqtt,), (), (Listener("0.0.0.0", 1883, None),), {}, probed=True)

    assert rows == (Row("ice-colder-mqtt", KIND_TCP, "0.0.0.0:1883", "ice-colder-mqtt", "", "LISTENING"),)


def test_tcp_row_is_down_when_nothing_listens():
    mqtt = Container("ice-colder-mqtt", (("0.0.0.0", 1883),), {"harbor.kind": "tcp", "harbor.port": "1883"})

    rows = build_rows((mqtt,), (), (), {}, probed=True)

    assert rows[0].state == "DOWN"


def test_tcp_row_with_a_bad_port_label_is_route_error():
    mqtt = Container("mqtt", (), {"harbor.kind": "tcp", "harbor.port": "lots"})

    assert build_rows((mqtt,), (), (), {}, probed=True)[0].state == "ROUTE ERROR"


def test_internal_row():
    db = Container("gte-db-1", (), {"harbor.kind": "internal", "harbor.description": "postgres"})

    rows = build_rows((db,), (), (), {}, probed=True)

    assert rows == (Row("gte-db-1", KIND_INTERNAL, "", "gte-db-1", "postgres", "INTERNAL"),)


def test_edge_row_is_listening_when_both_ports_are_held():
    edge = Container("traefik", (("100.69.239.123", 80), ("100.69.239.123", 443)), {"harbor.kind": "edge"})
    listeners = (Listener("100.69.239.123", 80, None), Listener("100.69.239.123", 443, None))

    rows = build_rows((edge,), (), listeners, {}, probed=True)

    assert rows[0].kind == KIND_EDGE
    assert rows[0].target == "100.69.239.123:80, 100.69.239.123:443"
    assert rows[0].state == "LISTENING"


def test_edge_row_is_down_when_one_port_is_missing():
    edge = Container("traefik", (("100.69.239.123", 80), ("100.69.239.123", 443)), {"harbor.kind": "edge"})

    rows = build_rows((edge,), (), (Listener("100.69.239.123", 80, None),), {}, probed=True)

    assert rows[0].state == "DOWN"


def test_undeclared_containers_produce_no_row():
    assert build_rows((Container("mystery", ()),), (), (), {}, probed=True) == ()


def test_rows_sort_by_name():
    a = http_container("zz-1", route="zeta", host="zeta.hpz440.ohr3023.org")
    b = Container("aa-1", (), {"harbor.kind": "internal"})

    assert [r.name for r in build_rows((a, b), (), (), {}, probed=True)] == ["aa-1", "zeta"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_directory.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write `src/harbor_console/directory.py`**

```python
"""The directory and its findings: what is declared, what is running, where they disagree.

Pure: containers, routers, listeners and probe results in; rows and findings
out. No I/O, so every rule is testable with plain values.

A container declares itself with labels and nothing else. `traefik.enable=true`
plus a router rule is an HTTP service; `harbor.kind` covers everything Traefik
does not route. Traefik is the truth for whether a route is live; this module
only joins its verdict to the container that asked for it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from harbor_console.docker import Container
from harbor_console.listening import Listener, addrs_overlap
from harbor_console.probe import Health
from harbor_console.traefik import TRAEFIK_UNAVAILABLE, Router, router_name

KIND_HTTP = "http"
KIND_TCP = "tcp"
KIND_INTERNAL = "internal"
KIND_EDGE = "edge"
KINDS = frozenset({KIND_TCP, KIND_INTERNAL, KIND_EDGE})

LABEL_ENABLE = "traefik.enable"
LABEL_KIND = "harbor.kind"
LABEL_PORT = "harbor.port"
LABEL_DESCRIPTION = "harbor.description"

STATE_UP = "UP"
STATE_DOWN = "DOWN"
STATE_ROUTE_ERROR = "ROUTE ERROR"
STATE_LISTENING = "LISTENING"
STATE_INTERNAL = "INTERNAL"
STATE_UNKNOWN = "UNKNOWN"

_RULE_LABEL = re.compile(r"^traefik\.http\.routers\.([^.]+)\.rule$")
_HOST = re.compile(r"Host\(`([^`]+)`\)")


@dataclass(frozen=True)
class Row:
    """One directory entry."""

    name: str
    kind: str
    target: str
    container: str
    description: str
    state: str


def declared_kind(container: Container) -> str | None:
    """Which kind a container declares, or None when it declares nothing."""
    if container.labels.get(LABEL_ENABLE, "").lower() == "true":
        return KIND_HTTP
    kind = container.labels.get(LABEL_KIND)
    return kind if kind in KINDS else None


def route_of(container: Container) -> tuple[str, str | None] | None:
    """The `(router name, host)` an HTTP container asks Traefik for.

    The router name is the label's, not the container's: it is the key
    Traefik reports under, and the name the page shows. With `traefik.enable`
    but no rule label Traefik would invent a router named for the container;
    the page names it the same way and leaves the host unknown, which
    `build_rows` reports as a route error rather than guessing a URL.
    """
    if declared_kind(container) != KIND_HTTP:
        return None
    for key, value in sorted(container.labels.items()):
        match = _RULE_LABEL.match(key)
        if match:
            host = _HOST.search(value)
            return match.group(1), host.group(1) if host else None
    return container.name, None


def build_rows(
    containers: Sequence[Container],
    routers: Sequence[Router],
    listeners: Sequence[Listener],
    health: Mapping[str, Health],
    probed: bool,
) -> tuple[Row, ...]:
    """One row per declared container, ordered by name."""
    by_name = {} if routers is TRAEFIK_UNAVAILABLE else {r.name: r for r in routers}
    rows = []
    for container in containers:
        kind = declared_kind(container)
        if kind is None:
            continue
        description = container.labels.get(LABEL_DESCRIPTION, "")
        if kind == KIND_HTTP:
            rows.append(_http_row(container, by_name, health, probed, description))
        elif kind == KIND_TCP:
            rows.append(_tcp_row(container, listeners, description))
        elif kind == KIND_EDGE:
            rows.append(_edge_row(container, listeners, description))
        else:
            rows.append(
                Row(container.name, KIND_INTERNAL, "", container.name, description, STATE_INTERNAL)
            )
    return tuple(sorted(rows, key=lambda row: row.name))


def _http_row(
    container: Container,
    routers: Mapping[str, Router],
    health: Mapping[str, Health],
    probed: bool,
    description: str,
) -> Row:
    name, host = route_of(container)  # type: ignore[misc] - kind is HTTP here
    router = routers.get(router_name(name))
    target = f"https://{host}/" if host else ""
    if host is None or (router is not None and not router.enabled):
        state = STATE_ROUTE_ERROR
    elif not probed:
        state = STATE_UNKNOWN
    else:
        result = health.get(name)
        state = STATE_UP if result is not None and result.up else STATE_DOWN
    return Row(name, KIND_HTTP, target, container.name, description, state)


def _tcp_row(container: Container, listeners: Sequence[Listener], description: str) -> Row:
    try:
        port = int(container.labels.get(LABEL_PORT, ""))
    except ValueError:
        return Row(container.name, KIND_TCP, "", container.name, description, STATE_ROUTE_ERROR)
    published = [pair for pair in container.published if pair[1] == port]
    addr = published[0][0] if published else "0.0.0.0"
    held = any(
        listener.port == port and addrs_overlap(listener.addr, addr) for listener in listeners
    )
    return Row(
        container.name,
        KIND_TCP,
        f"{addr}:{port}",
        container.name,
        description,
        STATE_LISTENING if held else STATE_DOWN,
    )


def _edge_row(container: Container, listeners: Sequence[Listener], description: str) -> Row:
    held = all(
        any(
            listener.port == port and addrs_overlap(listener.addr, addr)
            for listener in listeners
        )
        for addr, port in container.published
    ) and bool(container.published)
    target = ", ".join(f"{addr}:{port}" for addr, port in container.published)
    return Row(
        container.name,
        KIND_EDGE,
        target,
        container.name,
        description,
        STATE_LISTENING if held else STATE_DOWN,
    )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_directory.py -v`
Expected: 21 passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/directory.py tests/test_directory.py
git commit -m "feat(directory): rows from labels, routers and listeners

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: `directory.py` — findings

**Files:**
- Modify: `src/harbor_console/directory.py`
- Test: `tests/test_directory.py`

**Interfaces:**
- Produces: `Finding(kind: str, detail: str)`, frozen. Constants `UNDECLARED_CONTAINER = "undeclared-container"`, `BYPASSES_PROXY = "bypasses-proxy"`, `ROUTE_ERROR = "route-error"`, `UNDECLARED_TAILNET_LISTENER = "undeclared-tailnet-listener"`. `EPHEMERAL_MIN = 32768`, `EPHEMERAL_MAX = 60999`.
- `find_findings(containers, routers, listeners, tailnet_address: str | None, own_port: int | None = None) -> tuple[Finding, ...]`. `containers` may be `DOCKER_UNAVAILABLE`; `routers` may be `TRAEFIK_UNAVAILABLE`. `own_port` is the page's own bind, which no container publishes and is never a finding.

- [ ] **Step 1: Append to `tests/test_directory.py`**

```python
from harbor_console.directory import (
    BYPASSES_PROXY,
    ROUTE_ERROR,
    UNDECLARED_CONTAINER,
    UNDECLARED_TAILNET_LISTENER,
    Finding,
    find_findings,
)
from harbor_console.docker import DOCKER_UNAVAILABLE

TAILNET = "100.69.239.123"


def test_a_clean_host_has_no_findings():
    containers = (
        http_container(),
        Container("ice-colder-mqtt", (("0.0.0.0", 1883),), {"harbor.kind": "tcp", "harbor.port": "1883"}),
        Container("gte-db-1", (), {"harbor.kind": "internal"}),
        Container("traefik", ((TAILNET, 80), (TAILNET, 443)), {"harbor.kind": "edge"}),
    )
    routers = (Router("parksmart@docker", HOST, "parksmart", True, None),)
    listeners = (Listener(TAILNET, 80, None), Listener(TAILNET, 443, None), Listener("0.0.0.0", 1883, None))

    assert find_findings(containers, routers, listeners, TAILNET) == ()


def test_an_unlabelled_container_is_undeclared():
    findings = find_findings((Container("mystery", ()),), (), (), TAILNET)

    assert findings == (Finding(UNDECLARED_CONTAINER, "container 'mystery' carries no traefik.enable or harbor.kind label"),)


def test_an_http_container_still_publishing_a_port_bypasses_the_proxy():
    container = http_container()
    container = Container(container.name, (("127.0.0.1", 8000),), container.labels, container.networks)

    findings = find_findings((container,), (Router("parksmart@docker", HOST, "parksmart", True, None),), (), TAILNET)

    assert findings == (Finding(BYPASSES_PROXY, "container 'parksmart-parksmart-1' publishes 127.0.0.1:8000, which no harbor.kind=tcp label accounts for"),)


def test_an_undeclared_container_with_a_port_reports_both():
    findings = find_findings((Container("mystery", (("0.0.0.0", 9000),)),), (), (), TAILNET)

    assert [f.kind for f in findings] == [UNDECLARED_CONTAINER, BYPASSES_PROXY]


def test_a_tcp_container_publishing_its_declared_port_is_fine():
    mqtt = Container("mqtt", (("0.0.0.0", 1883),), {"harbor.kind": "tcp", "harbor.port": "1883"})

    assert find_findings((mqtt,), (), (), TAILNET) == ()


def test_a_tcp_container_publishing_an_extra_port_bypasses():
    mqtt = Container("mqtt", (("0.0.0.0", 1883), ("0.0.0.0", 9001)), {"harbor.kind": "tcp", "harbor.port": "1883"})

    findings = find_findings((mqtt,), (), (), TAILNET)

    assert findings == (Finding(BYPASSES_PROXY, "container 'mqtt' publishes 0.0.0.0:9001, which no harbor.kind=tcp label accounts for"),)


def test_the_edge_may_publish_anything():
    edge = Container("traefik", ((TAILNET, 80), (TAILNET, 443), ("127.0.0.1", 8081)), {"harbor.kind": "edge"})

    assert find_findings((edge,), (), (), TAILNET) == ()


def test_a_disabled_router_is_a_route_error():
    routers = (Router("parksmart@docker", HOST, "parksmart", False, 'service "parksmart@docker" does not exist'),)

    findings = find_findings((http_container(),), routers, (), TAILNET)

    assert findings == (Finding(ROUTE_ERROR, 'router parksmart@docker is disabled: service "parksmart@docker" does not exist'),)


def test_an_http_container_traefik_does_not_know_is_a_route_error():
    findings = find_findings((http_container(),), (), (), TAILNET)

    assert findings == (Finding(ROUTE_ERROR, "container 'parksmart-parksmart-1' asks for router parksmart@docker, which Traefik does not report; is it on the harbor network?"),)


def test_route_errors_are_withheld_when_traefik_is_unavailable():
    assert find_findings((http_container(),), TRAEFIK_UNAVAILABLE, (), TAILNET) == ()


def test_container_findings_are_withheld_when_docker_is_unavailable():
    findings = find_findings(DOCKER_UNAVAILABLE, (), (Listener(TAILNET, 9999, None),), TAILNET)

    assert findings == ()


def test_a_host_process_on_the_tailnet_is_undeclared():
    findings = find_findings((), (), (Listener(TAILNET, 8443, None),), TAILNET)

    assert findings == (Finding(UNDECLARED_TAILNET_LISTENER, f"{TAILNET}:8443 is listening on the tailnet, and no container publishes it"),)


def test_a_tailnet_listener_a_container_publishes_is_not_reported():
    edge = Container("traefik", ((TAILNET, 443),), {"harbor.kind": "edge"})

    assert find_findings((edge,), (), (Listener(TAILNET, 443, None),), TAILNET) == ()


def test_a_wildcard_listener_is_not_a_tailnet_finding():
    assert find_findings((), (), (Listener("0.0.0.0", 22, None),), TAILNET) == ()


def test_an_ephemeral_tailnet_port_is_not_reported():
    assert find_findings((), (), (Listener(TAILNET, 53678, None),), TAILNET) == ()


def test_tailnet_findings_are_withheld_without_a_tailnet_address():
    assert find_findings((), (), (Listener(TAILNET, 8443, None),), None) == ()


def test_the_pages_own_port_is_not_an_undeclared_listener():
    findings = find_findings((), (), (Listener(TAILNET, 8100, None),), TAILNET, own_port=8100)

    assert findings == ()


def test_findings_come_in_a_stable_order():
    containers = (
        Container("mystery", (("0.0.0.0", 9000),)),
        http_container(),
    )
    listeners = (Listener(TAILNET, 8443, None),)

    kinds = [f.kind for f in find_findings(containers, (), listeners, TAILNET)]

    assert kinds == [UNDECLARED_CONTAINER, BYPASSES_PROXY, ROUTE_ERROR, UNDECLARED_TAILNET_LISTENER]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_directory.py -v`
Expected: FAIL — `ImportError: cannot import name 'BYPASSES_PROXY'`.

- [ ] **Step 3: Append to `src/harbor_console/directory.py`**

Add to imports: `from harbor_console.docker import DOCKER_UNAVAILABLE, Container`. Then append:

```python
UNDECLARED_CONTAINER = "undeclared-container"
BYPASSES_PROXY = "bypasses-proxy"
ROUTE_ERROR = "route-error"
UNDECLARED_TAILNET_LISTENER = "undeclared-tailnet-listener"

#: Linux's default local port range. A tailnet listener in here is a
#: kernel-assigned port nobody chose -- tailscaled's own peerapi lands in it
#: on a different port after every restart -- not a deliberate publish.
EPHEMERAL_MIN = 32768
EPHEMERAL_MAX = 60999


@dataclass(frozen=True)
class Finding:
    """One way the declarations and the host disagree."""

    kind: str
    detail: str


def find_findings(
    containers: Sequence[Container],
    routers: Sequence[Router],
    listeners: Sequence[Listener],
    tailnet_address: str | None,
    own_port: int | None = None,
) -> tuple[Finding, ...]:
    """Every disagreement, in a stable order.

    `own_port` is this page's own bind on the tailnet address: a host process
    no container publishes, and the one such listener that is declared by
    being this program.

    Container findings need container evidence and are withheld when Docker
    could not be read; route findings need Traefik's verdict and are withheld
    when it could not be asked. Absence of evidence is never a finding.
    """
    findings: list[Finding] = []
    docker_available = containers is not DOCKER_UNAVAILABLE
    traefik_available = routers is not TRAEFIK_UNAVAILABLE

    if docker_available:
        for container in sorted(containers, key=lambda c: c.name):
            if declared_kind(container) is None:
                findings.append(
                    Finding(
                        UNDECLARED_CONTAINER,
                        f"container '{container.name}' carries no {LABEL_ENABLE} or {LABEL_KIND} label",
                    )
                )
        for container in sorted(containers, key=lambda c: c.name):
            for addr, port in _unaccounted_ports(container):
                findings.append(
                    Finding(
                        BYPASSES_PROXY,
                        f"container '{container.name}' publishes {addr}:{port}, "
                        f"which no {LABEL_KIND}={KIND_TCP} label accounts for",
                    )
                )

    if docker_available and traefik_available:
        known = {router.name: router for router in routers}
        for container in sorted(containers, key=lambda c: c.name):
            route = route_of(container)
            if route is None:
                continue
            name = router_name(route[0])
            router = known.get(name)
            if router is None:
                findings.append(
                    Finding(
                        ROUTE_ERROR,
                        f"container '{container.name}' asks for router {name}, which "
                        "Traefik does not report; is it on the harbor network?",
                    )
                )
            elif not router.enabled:
                findings.append(
                    Finding(ROUTE_ERROR, f"router {name} is disabled: {router.error or 'no reason given'}")
                )

    if docker_available and tailnet_address is not None:
        published = [pair for container in containers for pair in container.published]
        for addr, port in sorted({(l.addr, l.port) for l in listeners if l.addr == tailnet_address}):
            if port == own_port or EPHEMERAL_MIN <= port <= EPHEMERAL_MAX:
                continue
            if any(p == port and addrs_overlap(a, addr) for a, p in published):
                continue
            findings.append(
                Finding(
                    UNDECLARED_TAILNET_LISTENER,
                    f"{addr}:{port} is listening on the tailnet, and no container publishes it",
                )
            )

    return tuple(findings)


def _unaccounted_ports(container: Container) -> list[tuple[str, int]]:
    """Published host ports no label explains. The edge may publish anything."""
    kind = declared_kind(container)
    if kind == KIND_EDGE:
        return []
    allowed: set[int] = set()
    if kind == KIND_TCP:
        try:
            allowed.add(int(container.labels.get(LABEL_PORT, "")))
        except ValueError:
            pass
    return [pair for pair in container.published if pair[1] not in allowed]
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_directory.py -v`
Expected: 39 passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/directory.py tests/test_directory.py
git commit -m "feat(directory): findings for undeclared, bypassing and broken routes

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: `snapshot.py` and `web.py` render the directory

**Files:**
- Modify: `src/harbor_console/snapshot.py`
- Modify: `src/harbor_console/web.py`
- Test: `tests/test_web.py` (rewrite)

**Interfaces:**
- Produces `Snapshot(collected, metrics, rows: tuple[Row,...]=(), findings: tuple[Finding,...]=(), listeners=(), containers=(), docker_available=True, traefik_available=True, health: dict[str, Health]={}, collection_error=None, probed=False, tailnet_address=None)`.
- `web.render_page(snapshot) -> bytes`; `web.make_handler(get_snapshot)` serving `/` and `/index.html` only; everything else 404.
- Removes `Drift`, `ports_payload`, `_ports_refusals`, `UNPROBED_REASON`, `DOCKER_REASON`, `STALE_REASON`, `_lease_has_listener`, `_address_cell`, `_ledger_written_text`.

- [ ] **Step 1: Rewrite `tests/test_web.py`**

Keep `_get(handler_cls, path)` from the existing file (lines 272–302: the in-memory socket harness). Replace everything else with:

```python
from datetime import datetime

from harbor_console import web
from harbor_console.directory import (
    KIND_HTTP,
    KIND_INTERNAL,
    KIND_TCP,
    UNDECLARED_CONTAINER,
    Finding,
    Row,
)
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
    assert "Nothing has been collected yet" in page


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


def test_handler_404s_an_unknown_path():
    assert _get(web.make_handler(snapshot), "/nope")[0] == 404


def test_handler_content_length_matches_the_body():
    _status, headers, body = _get(web.make_handler(snapshot), "/")

    assert int(headers["Content-Length"]) == len(body)


def test_handler_answers_500_rather_than_nothing_when_rendering_raises():
    status, _headers, body = _get(web.make_handler(lambda: Snapshot(NOW, {})), "/")

    assert status == 500
    assert body == b"internal error\n"
```

If `_get` in the existing file returns a different shape than `(status, headers, body)`, adapt these assertions to its shape rather than changing `_get`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_web.py -v`
Expected: FAIL — `TypeError: Snapshot.__init__() got an unexpected keyword argument 'rows'`.

- [ ] **Step 3: Rewrite `src/harbor_console/snapshot.py`**

```python
"""The contract between the prober and the renderer.

Data only. The prober publishes one of these on an interval; the handler reads
the last one and renders it. Its own module so neither side imports the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from harbor_console.directory import Finding, Row
from harbor_console.docker import Container
from harbor_console.listening import Listener
from harbor_console.probe import Health


@dataclass(frozen=True)
class Snapshot:
    """Everything the page shows, collected at one moment.

    `frozen=True` is shallow: `metrics` and `health` are ordinary dicts. The
    prober publishes one, handlers only read it; build a new snapshot rather
    than editing one in place.

    `probed` separates "collected, found nothing" from "collected nothing
    yet". It defaults to False because that is the honest default.
    """

    collected: datetime
    metrics: dict[str, str | float | int]
    rows: tuple[Row, ...] = ()
    findings: tuple[Finding, ...] = ()
    listeners: tuple[Listener, ...] = ()
    containers: tuple[Container, ...] = ()
    docker_available: bool = True
    traefik_available: bool = True
    #: Keyed by `Row.name` -- the router name for HTTP rows.
    health: dict[str, Health] = field(default_factory=dict)
    #: Why the last collection cycle failed, whatever its source.
    collection_error: str | None = None
    probed: bool = False
    #: The tailnet address this process bound. None only outside `webapp.main`.
    tailnet_address: str | None = None
```

- [ ] **Step 4: Rewrite `src/harbor_console/web.py`**

```python
"""Rendering the status page, and serving it.

Renders a snapshot and nothing else -- it never collects and never probes.
Probing happens in a background thread and the handler only reads the last
published snapshot, so one hung service cannot make the page slow.

The page is read-only: no forms, no buttons, no state-changing routes.
"""

from __future__ import annotations

from collections.abc import Callable
from html import escape
from http.server import BaseHTTPRequestHandler

from harbor_console.directory import KIND_HTTP, STATE_DOWN, STATE_ROUTE_ERROR, Row
from harbor_console.snapshot import Snapshot

REFRESH_SECONDS = 30

_STYLE = """
body { font-family: ui-monospace, monospace; margin: 2rem; max-width: 60rem; }
h1, h2 { font-weight: 600; }
table { border-collapse: collapse; width: 100%; margin-bottom: 2rem; }
td, th { text-align: left; padding: 0.25rem 0.75rem 0.25rem 0; vertical-align: top; }
tr.detail td { padding-left: 2rem; opacity: 0.75; }
.down { font-weight: 700; }
.banner { border: 1px solid; padding: 0.5rem 0.75rem; margin-bottom: 1.5rem; }
.stamp { opacity: 0.7; }
"""


def render_page(snapshot: Snapshot) -> bytes:
    """Render the whole status page as one self-contained document."""
    parts = [
        "<!doctype html><html><head><meta charset=\"utf-8\">",
        f"<meta http-equiv=\"refresh\" content=\"{REFRESH_SECONDS}\">",
        "<title>Harbor Console</title>",
        f"<style>{_STYLE}</style></head><body>",
        f"<h1>{escape(str(snapshot.metrics['hostname']))}</h1>",
    ]
    if snapshot.collection_error is not None:
        tail = " Showing the last good page." if snapshot.probed else ""
        parts.append(
            f"<p class=\"banner\">The last collection cycle failed: "
            f"{escape(snapshot.collection_error)}.{tail}</p>"
        )
    if not snapshot.docker_available:
        parts.append(
            "<p class=\"banner\">Docker could not be read, so undeclared containers "
            "and bypassed routes are not reported.</p>"
        )
    if not snapshot.traefik_available:
        parts.append(
            "<p class=\"banner\">Traefik could not be read, so route errors are not "
            "reported and HTTP rows show only what the probe saw.</p>"
        )
    parts.append(_host_table(snapshot))
    parts.append(_directory_table(snapshot))
    parts.append(_findings_section(snapshot))
    parts.append(
        f"<p class=\"stamp\">Collected "
        f"{escape(snapshot.collected.strftime('%Y-%m-%d %H:%M:%S'))}, "
        f"refreshing every {REFRESH_SECONDS}s.</p>"
    )
    parts.append("</body></html>")
    return "".join(parts).encode("utf-8")


def _host_table(snapshot: Snapshot) -> str:
    rows = [
        ("Uptime", snapshot.metrics["uptime"]),
        ("CPU", f"{float(snapshot.metrics['cpu_utilization']):.1f}%"),
        ("Memory", f"{float(snapshot.metrics['memory_utilization']):.1f}%"),
        ("Disk", f"{float(snapshot.metrics['disk_utilization']):.1f}%"),
        ("IPv4", snapshot.metrics["ipv4_address"]),
        ("Containers", snapshot.metrics["docker_container_count"]),
        ("Time", snapshot.metrics["current_datetime"]),
    ]
    if snapshot.tailnet_address is not None:
        rows.insert(5, ("Tailnet", snapshot.tailnet_address))
    cells = "".join(
        f"<tr><td>{escape(label)}</td><td>{escape(str(value))}</td></tr>"
        for label, value in rows
    )
    return f"<h2>Host</h2><table>{cells}</table>"


def _target_cell(row: Row) -> str:
    if row.kind == KIND_HTTP and row.target:
        return f"<a href=\"{escape(row.target)}\">{escape(row.target)}</a>"
    return escape(row.target)


def _state_cell(row: Row) -> str:
    if row.state in (STATE_DOWN, STATE_ROUTE_ERROR):
        return f"<span class=\"down\">{escape(row.state)}</span>"
    return escape(row.state)


def _directory_table(snapshot: Snapshot) -> str:
    if not snapshot.rows:
        return "<h2>Directory</h2><p>No services are declared.</p>"
    rows = []
    for row in snapshot.rows:
        rows.append(
            f"<tr><td>{escape(row.name)}</td><td>{escape(row.kind)}</td>"
            f"<td>{_target_cell(row)}</td><td>{_state_cell(row)}</td>"
            f"<td>{escape(row.container)}</td><td>{escape(row.description)}</td></tr>"
        )
        health = snapshot.health.get(row.name)
        if health is not None:
            if health.summary:
                rows.append(f"<tr class=\"detail\"><td colspan=\"6\">{escape(health.summary)}</td></tr>")
            for detail in health.detail:
                rows.append(
                    f"<tr class=\"detail\"><td colspan=\"6\">{escape(detail.label)}: "
                    f"{escape(detail.value)}</td></tr>"
                )
            if health.warning:
                rows.append(f"<tr class=\"detail\"><td colspan=\"6\">{escape(health.warning)}</td></tr>")
    note = (
        ""
        if snapshot.probed
        else "<p>Nothing has been collected yet: the first cycle has not completed.</p>"
    )
    return (
        "<h2>Directory</h2>" + note + "<table>"
        "<tr><th>Name</th><th>Kind</th><th>Where</th><th>State</th><th>Container</th><th></th></tr>"
        + "".join(rows) + "</table>"
    )


def _findings_section(snapshot: Snapshot) -> str:
    if not snapshot.probed:
        return (
            "<h2>Findings</h2><p>Nothing has been collected yet: the first cycle "
            "has not completed, so findings are unknown.</p>"
        )
    if not snapshot.findings:
        return (
            "<h2>Findings</h2><p>No findings: every container is declared and "
            "every route is live.</p>"
        )
    items = "".join(
        f"<li>{escape(item.kind)} &mdash; {escape(item.detail)}</li>" for item in snapshot.findings
    )
    return f"<h2>Findings</h2><ul>{items}</ul>"


def make_handler(get_snapshot: Callable[[], Snapshot]) -> type[BaseHTTPRequestHandler]:
    """Build a handler class that reads the latest snapshot and nothing else."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib's required name
            try:
                self._dispatch()
            except Exception:  # noqa: BLE001 - a request boundary
                self._send(500, "text/plain; charset=utf-8", b"internal error\n")

        def _dispatch(self) -> None:
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", render_page(get_snapshot()))
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found\n")

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            """Quiet by default; journald already timestamps what matters."""

    return Handler
```

- [ ] **Step 5: Run the suite**

Run: `uv run pytest tests/test_web.py tests/test_directory.py -v`
Expected: pass. `tests/test_webapp.py`, `tests/test_reconcile.py`, `tests/test_ports_live.py` now fail on the removed `Drift`/`leases`/`ports_payload`; Task 8 and Task 9 resolve them. Do not commit a red suite: proceed to Task 8 before committing, or commit with `tests/test_webapp.py` and `tests/test_reconcile.py` and `tests/test_ports_live.py` temporarily deleted in the same commit as Task 8's work. Preferred: finish Task 8, then commit Tasks 7 and 8 together.

---

### Task 8: `webapp.py` without the ledger

**Files:**
- Modify: `src/harbor_console/webapp.py`
- Test: `tests/test_webapp.py` (rewrite)

**Interfaces:**
- Produces: `WEB_PORT = 8100`. `collect_snapshot(now, collector=collect_system_metrics, listeners=listening_sockets, containers=running_containers, routers=traefik_routers, prober=probe, tailnet_address=None, own_port=WEB_PORT) -> Snapshot`. `starting_snapshot(host, now, tailnet_address=None)`. `probe_loop`, `SnapshotHolder`, `main(argv=None, server_factory=ThreadingHTTPServer, start_prober=None)` keep their shapes. `main` has one refusal: `TailnetUnavailable`. Bind failure still exits `EXIT_REFUSED`.
- Removes `own_lease`, `own_port`, `NotDeclared`, `AmbiguousDeclaration`, `LEDGER_PATH`, `read_ledger_mtime`, `WEB_PROJECT`, `WEB_PORT_NAME`.

- [ ] **Step 1: Rewrite `tests/test_webapp.py`**

Keep the existing `main` tests that check: refusal on `TailnetUnavailable`, refusal when `server_factory` raises `OSError`, that the prober starts only after a successful bind, and that `probe_loop` publishes a failure reason and clears it on the next good cycle. Delete every test about leases, `own_port`, ledger errors, or ambiguity. Replace the collection tests with:

```python
from datetime import datetime

from harbor_console import webapp
from harbor_console.directory import KIND_HTTP, ROUTE_ERROR, UNDECLARED_CONTAINER
from harbor_console.docker import DOCKER_UNAVAILABLE, Container
from harbor_console.listening import Listener
from harbor_console.probe import Health
from harbor_console.traefik import TRAEFIK_UNAVAILABLE, Router

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
HOST = "parksmart.hpz440.ohr3023.org"
PARKSMART = Container(
    "parksmart-parksmart-1",
    (),
    {"traefik.enable": "true", "traefik.http.routers.parksmart.rule": f"Host(`{HOST}`)"},
    frozenset({"harbor"}),
)
ROUTER = Router("parksmart@docker", HOST, "parksmart", True, None)


def collect(**overrides):
    kwargs = dict(
        now=NOW,
        collector=lambda: METRICS,
        listeners=lambda: (),
        containers=lambda: (PARKSMART,),
        routers=lambda: (ROUTER,),
        prober=lambda url: Health(True, "ok", "fine", (), None),
        tailnet_address="100.69.239.123",
    )
    kwargs.update(overrides)
    return webapp.collect_snapshot(**kwargs)


def test_collect_snapshot_gathers_every_source():
    snapshot = collect()

    assert snapshot.metrics == METRICS
    assert snapshot.docker_available is True
    assert snapshot.traefik_available is True
    assert snapshot.health["parksmart"].up is True
    assert [r.name for r in snapshot.rows] == ["parksmart"]
    assert snapshot.rows[0].state == "UP"
    assert snapshot.findings == ()
    assert snapshot.probed is True
    assert snapshot.collection_error is None


def test_http_rows_are_probed_at_their_route():
    seen = []

    def prober(url):
        seen.append(url)
        return Health(True, None, None, (), None)

    collect(prober=prober)

    assert seen == [f"https://{HOST}"]


def test_rows_without_a_host_are_not_probed():
    seen = []
    plain = Container("plain", (), {"traefik.enable": "true"})

    collect(containers=lambda: (plain,), routers=lambda: (), prober=lambda url: seen.append(url))

    assert seen == []


def test_non_http_rows_are_not_probed():
    seen = []
    mqtt = Container("mqtt", (("0.0.0.0", 1883),), {"harbor.kind": "tcp", "harbor.port": "1883"})

    collect(containers=lambda: (mqtt,), routers=lambda: (), prober=lambda url: seen.append(url))

    assert seen == []


def test_collect_snapshot_marks_docker_unavailable():
    snapshot = collect(containers=lambda: DOCKER_UNAVAILABLE)

    assert snapshot.docker_available is False
    assert snapshot.containers == ()
    assert snapshot.rows == ()
    assert snapshot.findings == ()


def test_collect_snapshot_marks_traefik_unavailable():
    snapshot = collect(routers=lambda: TRAEFIK_UNAVAILABLE)

    assert snapshot.traefik_available is False
    assert snapshot.rows[0].state == "UP"
    assert all(f.kind != ROUTE_ERROR for f in snapshot.findings)


def test_collect_snapshot_reports_findings():
    snapshot = collect(containers=lambda: (PARKSMART, Container("mystery", ())))

    assert [f.kind for f in snapshot.findings] == [UNDECLARED_CONTAINER]


def test_the_pages_own_bind_is_not_a_finding():
    snapshot = collect(listeners=lambda: (Listener("100.69.239.123", webapp.WEB_PORT, None),))

    assert snapshot.findings == ()


def test_starting_snapshot_has_looked_at_nothing():
    snapshot = webapp.starting_snapshot("hpz440", NOW, tailnet_address="100.69.239.123")

    assert snapshot.probed is False
    assert snapshot.rows == ()
    assert snapshot.metrics["hostname"] == "hpz440"
    assert snapshot.tailnet_address == "100.69.239.123"
```

For the retained `main` tests: they previously monkeypatched `webapp.load_leases` or wrote a `services.toml`; remove that setup, and where they asserted the bind address was `(address, lease.port)` assert `(address, webapp.WEB_PORT)` instead. The `TailnetUnavailable` test still monkeypatches `webapp.tailscale_address`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_webapp.py -v`
Expected: FAIL — `collect_snapshot() got an unexpected keyword argument 'routers'`.

- [ ] **Step 3: Rewrite `src/harbor_console/webapp.py`**

```python
"""The harbor-console-web entry point: one prober thread and one HTTP server.

One refusal, at startup: without a Tailscale address this process exits
non-zero rather than binding something broader. Binding *is* the access
control -- the page is an inventory of every service on the host -- so there
is no fallback address, no `--host`, and no dev mode (ADR 7). Traefik fronts
this page at its route; the direct bind stays tailnet-only.

After that, nothing takes the page down. Docker or Traefik being unreadable
is a banner. A cycle that fails leaves the last good snapshot standing with
the reason attached.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from http.server import ThreadingHTTPServer

from harbor_console.directory import KIND_HTTP, build_rows, find_findings, route_of, declared_kind
from harbor_console.docker import DOCKER_UNAVAILABLE, Container, running_containers
from harbor_console.listening import Listener, listening_sockets
from harbor_console.probe import Health, probe
from harbor_console.snapshot import Snapshot
from harbor_console.system import collect_system_metrics
from harbor_console.tailnet import TailnetUnavailable, tailscale_address
from harbor_console.traefik import TRAEFIK_UNAVAILABLE, Router, traefik_routers
from harbor_console.web import make_handler

#: Fixed. Traefik's file-provider route for `harbor.<host>` points here, and
#: there is deliberately no way to configure it (ADR 3, ADR 15).
WEB_PORT = 8100

PROBE_INTERVAL_SECONDS = 30.0

EXIT_OK = 0
EXIT_REFUSED = 1


class SnapshotHolder:
    """The last snapshot the prober published. One writer, many readers."""

    def __init__(self, initial: Snapshot) -> None:
        self._lock = threading.Lock()
        self._snapshot = initial

    def get(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def set(self, snapshot: Snapshot) -> None:
        with self._lock:
            self._snapshot = snapshot


def starting_snapshot(host: str, now: datetime, tailnet_address: str | None = None) -> Snapshot:
    """The page's first answer, standing only until the prober's first cycle."""
    return Snapshot(
        collected=now,
        metrics={
            "hostname": host,
            "uptime": "collecting",
            "cpu_utilization": 0.0,
            "memory_utilization": 0.0,
            "disk_utilization": 0.0,
            "ipv4_address": "collecting",
            "docker_container_count": 0,
            "current_datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        },
        tailnet_address=tailnet_address,
    )


def collect_snapshot(
    now: datetime,
    collector: Callable[[], dict[str, str | float | int]] = collect_system_metrics,
    listeners: Callable[[], tuple[Listener, ...]] = listening_sockets,
    containers: Callable[[], tuple[Container, ...]] = running_containers,
    routers: Callable[[], tuple[Router, ...]] = traefik_routers,
    prober: Callable[[str], Health] = probe,
    tailnet_address: str | None = None,
    own_port: int | None = WEB_PORT,
) -> Snapshot:
    """Gather every source once and fold it into one snapshot.

    Only HTTP rows with a known host are probed, at their route, so a green
    row proves the path through Traefik. The two sentinels are passed to the
    policy intact and only flattened into the snapshot for the renderer.
    """
    metrics = dict(collector())
    found = listeners()
    running = containers()
    routed = routers()

    health: dict[str, Health] = {}
    for container in running:
        if declared_kind(container) != KIND_HTTP:
            continue
        route = route_of(container)
        if route is None or route[1] is None:
            continue
        health[route[0]] = prober(f"https://{route[1]}")

    return Snapshot(
        collected=now,
        metrics=metrics,
        rows=build_rows(running, routed, found, health, probed=True),
        findings=find_findings(running, routed, found, tailnet_address, own_port=own_port),
        listeners=found,
        containers=tuple(running),
        docker_available=running is not DOCKER_UNAVAILABLE,
        traefik_available=routed is not TRAEFIK_UNAVAILABLE,
        health=health,
        collection_error=None,
        probed=True,
        tailnet_address=tailnet_address,
    )


def probe_loop(
    holder: SnapshotHolder,
    collect: Callable[[], Snapshot],
    sleep: Callable[[float], None] = time.sleep,
    interval: float = PROBE_INTERVAL_SECONDS,
) -> None:
    """Publish a fresh snapshot on an interval until interrupted.

    A collection failure never takes the page down: the last good snapshot
    stands, with the reason attached. A successful cycle publishes
    `collection_error=None`, so a reason never outlives its cause.
    """
    while True:
        try:
            holder.set(collect())
        except Exception as exc:  # noqa: BLE001 - a supervisor loop, see above
            holder.set(replace(holder.get(), collection_error=str(exc) or exc.__class__.__name__))
        try:
            sleep(interval)
        except KeyboardInterrupt:
            return


def main(
    argv: list[str] | None = None,
    server_factory: Callable[..., object] = ThreadingHTTPServer,
    start_prober: Callable[[SnapshotHolder, str], None] | None = None,
) -> int:
    """Entry point. Refuses to start rather than binding anything broader."""
    try:
        address = tailscale_address()
    except TailnetUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    host = str(collect_system_metrics()["hostname"])
    holder = SnapshotHolder(starting_snapshot(host, datetime.now(), tailnet_address=address))

    try:
        server = server_factory((address, WEB_PORT), make_handler(holder.get))
    except OSError as exc:
        print(f"error: could not bind {address}:{WEB_PORT}: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    if start_prober is None:
        start_prober = _default_prober
    start_prober(holder, address)

    print(f"harbor-console-web listening on http://{address}:{WEB_PORT}/")
    try:
        server.serve_forever()  # type: ignore[attr-defined]
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()  # type: ignore[attr-defined]
    return EXIT_OK


def _default_prober(holder: SnapshotHolder, tailnet_address: str) -> None:
    def collect() -> Snapshot:
        return collect_snapshot(datetime.now(), tailnet_address=tailnet_address)

    thread = threading.Thread(
        target=probe_loop, args=(holder, collect), name="harbor-prober", daemon=True
    )
    thread.start()


if __name__ == "__main__":
    raise SystemExit(main())
```

Note `main` calls `collect_system_metrics()` once for the hostname; that collector degrades and never raises, so it is safe on the startup path.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/test_webapp.py tests/test_web.py tests/test_directory.py -v`
Expected: pass. Remaining failures are only in files Task 9 deletes.

- [ ] **Step 5: Commit Tasks 7 and 8 together**

```bash
git add src/harbor_console/snapshot.py src/harbor_console/web.py src/harbor_console/webapp.py tests/test_web.py tests/test_webapp.py
git commit -m "feat(web): the page is a directory of labelled containers and Traefik routes

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Retire the ledger era

**Files:**
- Delete: `src/harbor_console/ports/` (whole package), `src/harbor_console/serve.py`, `src/harbor_console/addressing.py`, `src/harbor_console/reconcile.py`, `services.toml`, `.harbor.toml`, `HARBOR_PORTS.md`
- Delete tests: `tests/test_ports_*.py`, `tests/test_serve.py`, `tests/test_addressing.py`, `tests/test_reconcile.py`
- Modify: `pyproject.toml`, `.gitignore`, `src/harbor_console/app.py` (if it dispatches the `ports` subcommand)

- [ ] **Step 1: Check what `app.py` dispatches**

Run: `grep -n "ports" src/harbor_console/app.py src/harbor_console/__main__.py`
If `app.main` routes `ports` to `harbor_console.ports.cli`, remove that branch so `harbor-console` runs the dashboard only. The `__main__` dispatch keeps its shape.

- [ ] **Step 2: Delete**

```bash
git rm -r src/harbor_console/ports src/harbor_console/serve.py src/harbor_console/addressing.py src/harbor_console/reconcile.py services.toml .harbor.toml
git rm tests/test_ports_allocate.py tests/test_ports_atomic.py tests/test_ports_cli.py tests/test_ports_compose.py tests/test_ports_declaration.py tests/test_ports_discovery.py tests/test_ports_envfile.py tests/test_ports_explainer.py tests/test_ports_ledger.py tests/test_ports_live.py tests/test_serve.py tests/test_addressing.py tests/test_reconcile.py
rm HARBOR_PORTS.md
```

In `.gitignore` remove the `.harbor-tmp.*` line. In `src/harbor_console/listening.py` nothing changes. In `src/harbor_console/ports/keys.py` — gone with the package.

- [ ] **Step 3: Verify nothing imports the dead modules**

Run: `grep -rn "harbor_console.ports\|harbor_console.serve\|harbor_console.addressing\|harbor_console.reconcile" src tests`
Expected: no output.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest`
Expected: all pass, no collection errors. Then `uv run harbor-console --help` (or `uv run python -c "import harbor_console.app"`) imports cleanly.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: retire the port ledger, allocator and tailscale serve collector

Traefik is the only truth for routes and labels are the only declaration
(ADR 15). Everything that reserved, allocated or reconciled host ports
goes with services.toml.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: ADR 15 and the docs

**Files:**
- Create: `docs/adr/0015-reverse-proxy-and-label-declared-services.md`
- Modify: `docs/adr/0008-…` through `docs/adr/0014-…` (Status line only), `docs/adr/README.md` (index), `CLAUDE.md`, `README.md`, `docs/architecture.md`, `docs/deployment.md`

- [ ] **Step 1: Write the ADR**

```markdown
# 15. A reverse proxy fronts every HTTP service; labels are the only declaration

Date: 2026-09-18

## Status

Accepted. Supersedes ADR 8, 9, 10, 11, 13 and 14; amends ADR 6, 7 and 12.

## Context

The lease ledger reserved host ports, but only for projects that declared a
`.harbor.toml` and ran `ports sync`. On 2026-09-18 a review found
`parksmart-parksmart-1` running on `127.0.0.1:8000` under the lease of the
not-running `fastapi-docker/api`; the page reported the dead lease as
LISTENING and no drift. Projects with no declaration never appeared.

Host ports were the scarce resource only because every service published
one. With a reverse proxy in front, no HTTP service publishes a port at all;
the scarce resource is the hostname, and the proxy knows every live one.

## Decision

- Traefik runs from `deploy/traefik/compose.yaml`, publishing
  `100.69.239.123:80` and `:443` only. The bind is still the access control
  (ADR 7); there is no `0.0.0.0` and no LAN exposure.
- One wildcard certificate for `*.hpz440.ohr3023.org` via Cloudflare
  DNS-01. Routes are `<name>.hpz440.ohr3023.org`.
- Containers declare themselves with compose labels and nothing else:
  `traefik.enable=true` plus a `Host()` rule for HTTP; `harbor.kind=tcp` and
  `harbor.port` for a TCP service that keeps a published port;
  `harbor.kind=internal` for one that publishes nothing; `harbor.kind=edge`
  for the proxy. Optional `harbor.description`.
- Traefik is the only truth for which routes are live. The page reads its
  API on loopback and Docker's labels, and reports: undeclared containers,
  containers that bypass the proxy with a raw port, routers Traefik rejects,
  and host processes on the tailnet no container publishes.
- The ledger, the allocator, `.harbor.toml`, `HARBOR_PORTS.md`, `/ports.json`
  and the `tailscale serve` collector are removed. The page binds fixed port
  8100 on the tailnet address and is reached at `harbor.hpz440.ohr3023.org`.

## Consequences

- A new service is one compose edit: labels and a network. No sync step, no
  file in this repo to update, no second declaration to drift.
- A service that still publishes a port is a finding, not a silent squat.
  The ParkSmart case cannot recur: it either has a route or it is reported.
- TLS is real everywhere, so Secure cookies work without `tailscale serve`.
- The proxy is a single point of failure for every HTTP service. It restarts
  with Docker and the page reports it as an edge row.
- Non-HTTP services are listed but not routed; MQTT stays on a published
  port by declaration.
- ADRs 8–11, 13 and 14 describe machinery that no longer exists. They stay
  as the record of why it was built and what it caught.
```

- [ ] **Step 2: Mark the superseded ADRs**

In each of `docs/adr/0008-…md`, `0009-…md`, `0010-…md`, `0011-…md`, `0013-…md`, `0014-…md`, change the `## Status` body to `Superseded by [ADR-0015](0015-reverse-proxy-and-label-declared-services.md)`. Add the 0015 line to `docs/adr/README.md`'s index in the existing format.

- [ ] **Step 3: Rewrite the project docs**

`CLAUDE.md`: replace the "What this is" bullets, the Commands table (drop the three `ports` rows), the "Architecture" web-surface list (replace `serve.py`, `reconcile.py`, `addressing.py` entries with `traefik.py` and `directory.py`; remove the `ports/` list), the three "load-bearing behaviours" of the service (replace with: the page binds tailnet:8100, one refusal, Traefik/Docker unreadable is a banner), the `.harbor-tmp.*` section (delete), the "Hard constraints" (drop atomic-write and env-last; add "Traefik publishes the tailnet address only; the API is loopback only"), and "Scope discipline" (replace the two answered questions with a pointer to ADR 15). Keep the tty dashboard, DI, and graceful-degradation sections. Add the label convention table from the spec under a new "Declaring a service" heading.

`README.md`: same edits at the user level, plus the label table.

`docs/architecture.md`: replace the web-surface module list to match.

`docs/deployment.md`: replace the "port 80 / CAP_NET_BIND_SERVICE" section with the Traefik section from Task 11 (network, `/etc/traefik/env`, DNS record, `install.sh` starting Traefik), and the troubleshooting row for `Permission denied` on port 80 with one for `could not bind 100.69.239.123:8100`.

- [ ] **Step 4: Commit**

```bash
git add docs CLAUDE.md README.md
git commit -m "docs: ADR 15, reverse proxy and label-declared services

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Traefik deployment files

**Files:**
- Create: `deploy/traefik/compose.yaml`, `deploy/traefik/dynamic/harbor.yml.in`, `deploy/traefik/.gitignore`
- Modify: `deploy/harbor-console-web.service`, `deploy/install.sh`, `deploy/uninstall.sh`

- [ ] **Step 1: `deploy/traefik/compose.yaml`**

```yaml
# The edge. Publishes the tailnet address only (ADR 7, ADR 15). The API is
# loopback only; the page reads it there. `.env` beside this file is written
# by install.sh from the host's tailnet address and /etc/traefik/env.
services:
  traefik:
    image: traefik:v3.3
    container_name: traefik
    restart: unless-stopped
    command:
      - --providers.docker=true
      - --providers.docker.exposedbydefault=false
      - --providers.docker.network=harbor
      - --providers.file.directory=/etc/traefik/dynamic
      - --providers.file.watch=true
      - --entrypoints.web.address=:80
      - --entrypoints.web.http.redirections.entrypoint.to=websecure
      - --entrypoints.web.http.redirections.entrypoint.scheme=https
      - --entrypoints.websecure.address=:443
      - --entrypoints.websecure.http.tls=true
      - --entrypoints.websecure.http.tls.certresolver=cloudflare
      - --entrypoints.websecure.http.tls.domains[0].main=hpz440.ohr3023.org
      - --entrypoints.websecure.http.tls.domains[0].sans=*.hpz440.ohr3023.org
      - --entrypoints.traefik.address=:8081
      - --api.insecure=true
      - --certificatesresolvers.cloudflare.acme.email=${ACME_EMAIL}
      - --certificatesresolvers.cloudflare.acme.storage=/letsencrypt/acme.json
      - --certificatesresolvers.cloudflare.acme.dnschallenge=true
      - --certificatesresolvers.cloudflare.acme.dnschallenge.provider=cloudflare
      - --certificatesresolvers.cloudflare.acme.dnschallenge.resolvers=1.1.1.1:53
      - --log.level=INFO
    ports:
      - "${TAILNET_ADDRESS}:80:80"
      - "${TAILNET_ADDRESS}:443:443"
      - "127.0.0.1:8081:8081"
    env_file:
      - /etc/traefik/env
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./dynamic:/etc/traefik/dynamic:ro
      - letsencrypt:/letsencrypt
    networks:
      - harbor
    labels:
      harbor.kind: edge
      harbor.description: "Reverse proxy; every HTTPS route on this host"

volumes:
  letsencrypt:

networks:
  harbor:
    external: true
```

- [ ] **Step 2: `deploy/traefik/dynamic/harbor.yml.in`**

```yaml
# Rendered to harbor.yml by install.sh. The page is a systemd process, not a
# container, so it carries no labels; this is its declaration.
http:
  routers:
    harbor:
      rule: Host(`harbor.hpz440.ohr3023.org`)
      entryPoints:
        - websecure
      service: harbor
  services:
    harbor:
      loadBalancer:
        servers:
          - url: http://@TAILNET_ADDRESS@:8100
```

`deploy/traefik/.gitignore`:

```
.env
dynamic/harbor.yml
```

- [ ] **Step 3: `deploy/harbor-console-web.service`**

Delete the comment block and the two lines `AmbientCapabilities=CAP_NET_BIND_SERVICE` and `CapabilityBoundingSet=CAP_NET_BIND_SERVICE`. Add `After=docker.service` to the `After=` line. Nothing else changes.

- [ ] **Step 4: `deploy/install.sh`**

After the `usermod -aG docker harbor` line and before the unit loop, insert:

```bash
echo "==> Preparing the edge (Traefik)"
require_cmd docker "Install Docker first."
require_cmd tailscale "Install Tailscale first."
if [[ ! -f /etc/traefik/env ]]; then
  echo "Error: /etc/traefik/env is missing. Create it (mode 0600) with:" >&2
  echo "  CF_DNS_API_TOKEN=<cloudflare token with Zone.DNS edit on ohr3023.org>" >&2
  echo "  ACME_EMAIL=<address for Let's Encrypt notices>" >&2
  exit 1
fi
chmod 0600 /etc/traefik/env
TAILNET_ADDRESS=$(tailscale ip -4 | head -n1)
if [[ -z "${TAILNET_ADDRESS}" ]]; then
  echo "Error: tailscale ip -4 returned nothing; the edge binds the tailnet address only." >&2
  exit 1
fi
# shellcheck disable=SC1091
ACME_EMAIL=$(. /etc/traefik/env; echo "${ACME_EMAIL:-}")
if [[ -z "${ACME_EMAIL}" ]]; then
  echo "Error: ACME_EMAIL is not set in /etc/traefik/env." >&2
  exit 1
fi
docker network inspect harbor >/dev/null 2>&1 || docker network create harbor
TRAEFIK_DIR="${INSTALL_DIR}/deploy/traefik"
printf 'TAILNET_ADDRESS=%s\nACME_EMAIL=%s\n' "${TAILNET_ADDRESS}" "${ACME_EMAIL}" > "${TRAEFIK_DIR}/.env"
sed "s/@TAILNET_ADDRESS@/${TAILNET_ADDRESS}/" "${TRAEFIK_DIR}/dynamic/harbor.yml.in" > "${TRAEFIK_DIR}/dynamic/harbor.yml"
( cd "${TRAEFIK_DIR}" && docker compose up -d --remove-orphans )
```

Replace the closing echo about "the port services.toml leases it" with:

```bash
echo "The status page is https://harbor.hpz440.ohr3023.org/ (direct: http://${TAILNET_ADDRESS}:8100/)."
```

Also add `--exclude 'deploy/traefik/.env'` and `--exclude 'deploy/traefik/dynamic/harbor.yml'` to the rsync so a re-deploy does not delete the rendered files before they are re-rendered (they are re-rendered anyway; the exclude avoids a window where Traefik's watcher sees the file vanish).

- [ ] **Step 5: `deploy/uninstall.sh`**

Add, before the unit removal:

```bash
if [[ -f /opt/harbor-console/deploy/traefik/compose.yaml ]]; then
  echo "==> Stopping the edge (Traefik)"
  ( cd /opt/harbor-console/deploy/traefik && docker compose down ) || true
fi
```

Do not remove the `harbor` network or the `letsencrypt` volume; other projects join the network and the cert is expensive to re-issue.

- [ ] **Step 6: Validate the compose file locally against the LAN Docker context**

Run (from the repo root, Windows):

```
docker --context hpz440 compose -f deploy/traefik/compose.yaml --env-file NUL config
```

Expected: an error only about the missing `.env` variables or `/etc/traefik/env`; no YAML or schema error. If `--env-file NUL` is rejected, create `deploy/traefik/.env` locally with `TAILNET_ADDRESS=100.69.239.123` and `ACME_EMAIL=x@example.com` (it is gitignored), run `docker --context hpz440 compose -f deploy/traefik/compose.yaml config`, and expect rendered YAML with `100.69.239.123:80:80` in `ports`.

- [ ] **Step 7: Commit**

```bash
git add deploy
git commit -m "deploy: Traefik edge, page on 8100 without the low-port grant

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: Host cutover

This task runs on hpz440. Steps marked **sudo** need the `conrad` account (see memory: `gte` cannot sudo). Nothing here is reversible by `git revert`, so each step verifies before the next.

- [ ] **Step 1: DNS record**

In Cloudflare for `ohr3023.org`: add `A` record, name `*.hpz440`, content `100.69.239.123`, proxy status **DNS only**, TTL auto. Verify from the dev box:

```
nslookup parksmart.hpz440.ohr3023.org 1.1.1.1
```

Expected: `100.69.239.123`.

- [ ] **Step 2: Cloudflare token**

Create an API token scoped to `Zone.DNS: Edit` on `ohr3023.org` only. On the host, **sudo**:

```
sudo install -d -m 0755 /etc/traefik
sudo sh -c 'umask 077; printf "CF_DNS_API_TOKEN=%s\nACME_EMAIL=%s\n" "<token>" "<email>" > /etc/traefik/env'
```

- [ ] **Step 3: Remove the tailscale serve fronts** (**sudo**)

```
sudo tailscale serve status
sudo tailscale serve reset
sudo tailscale serve status
```

Expected after reset: no output. Portainer and GTE are still reachable on `127.0.0.1:9443` (host-only) and `hpz440:8080` until their migration in the next plan.

- [ ] **Step 4: Deploy** (**sudo**)

```
cd /srv/harbor-console
git pull
sudo bash deploy/install.sh
```

Expected output includes `Preparing the edge (Traefik)`, `docker compose up -d` creating `traefik`, and both units restarting. Then:

```
systemctl status harbor-console-web --no-pager
docker ps --filter name=traefik
docker logs traefik --since 2m
ss -ltnp | grep -E ':(80|443|8081|8100) '
```

Expected: page bound to `100.69.239.123:8100`; Traefik holding `100.69.239.123:80`, `100.69.239.123:443`, `127.0.0.1:8081`; Traefik log shows the ACME resolver obtaining `*.hpz440.ohr3023.org` (allow up to two minutes for DNS-01 propagation).

- [ ] **Step 5: Verify the route end to end**

From the dev box:

```
curl -sI https://harbor.hpz440.ohr3023.org/
curl -s http://127.0.0.1:8081/api/http/routers
```

The second runs on the host over ssh. Expected: first returns `HTTP/2 200` with a valid certificate; second lists `harbor@file` enabled.

If the first returns a Traefik 502/504, the container cannot reach the host's tailnet address. Check from inside: `docker exec traefik wget -qO- http://100.69.239.123:8100/ | head -c 100`. If that fails, report it; the fallback is `network_mode: host` for Traefik, which is a spec change and needs the user's decision.

- [ ] **Step 6: Read the page**

Open `https://harbor.hpz440.ohr3023.org/`. Expected directory: one `edge` row for `traefik` LISTENING. Expected findings: `undeclared-container` for every existing container, and `bypasses-proxy` for each published port. That list is the input to the migration plan.

- [ ] **Step 7: Record the deploy**

Nothing to commit here. Update the memory file `hpz440-deploy-checkout.md` if the deploy procedure changed (it did not: pull, then `sudo bash deploy/install.sh`).

---

## Self-review

**Spec coverage.** Edge: Task 11–12. Declaration convention: Task 5. Collectors `docker.py`/`traefik.py`/`probe.py`: Tasks 1, 2, 4. Policy rows and findings: Tasks 5–6. Snapshot/render/coordinate: Tasks 7–8. Retirement: Task 9. ADR and docs: Task 10. `listening.py`/`tailnet.py` unchanged except `addrs_overlap` moving in (Task 3). Migrations: separate plan `2026-09-18-reverse-proxy-migrations.md`.

**Type consistency.** `Container(name, published, labels, networks)` positional order used identically in Tasks 1, 5, 6, 8. `Router(name, host, service, enabled, error)` in Tasks 2, 5, 6, 8. `Row(name, kind, target, container, description, state)` in Tasks 5, 7. `Finding(kind, detail)` in Tasks 6, 7. `probe(base_url)` in Tasks 4, 8. `build_rows(containers, routers, listeners, health, probed)` and `find_findings(containers, routers, listeners, tailnet_address, own_port)` in Tasks 6, 8. `health` keyed by router label name in Tasks 5, 7, 8.

**Known seam.** Task 7 leaves the suite red until Task 8 lands; the two commit together. Task 9 is the only other point where deleted tests and deleted modules must go in one commit.
