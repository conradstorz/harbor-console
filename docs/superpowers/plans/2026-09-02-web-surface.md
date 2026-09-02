# Web Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `harbor-console-web`: a read-only status page served to the tailnet, plus the `/ports.json` endpoint the allocator already consumes.

**Architecture:** A background prober thread collects host state on an interval and publishes a frozen `Snapshot`; the HTTP handler only ever reads the last snapshot, so one hung service can never slow the page. Collectors, pure policy, renderer and coordinator are separate modules, matching the split the rest of the project uses.

**Tech Stack:** Python 3.13+, `psutil` (already a dependency), stdlib `http.server`, `urllib`, `subprocess`, `json`. `pytest`.

Implements `docs/superpowers/specs/2026-09-02-web-surface-design.md`.

## Global Constraints

- **No new runtime dependency.** `rich` and `psutil` are the only two, and this adds none. Stdlib `http.server`, `urllib`, `json`, `subprocess`, `ipaddress`. Never add `requests`, `flask`, `fastapi`, `uvicorn`, `docker`.
- **Python 3.13+**, `from __future__ import annotations` at the top of every module, full type hints, module and function docstrings — match `src/harbor_console/system.py`.
- **Tests use `pytest` with `monkeypatch`/`tmp_path`. No real sockets, no real HTTP, no real subprocesses, no real clock, no real Docker.** Every collector takes its side effect as an injected parameter. Tests live flat in `tests/`, named `test_<module>.py`.
- **Collectors never raise on a hostile environment** — they degrade to empty. The single deliberate exception is `tailnet.tailscale_address()`, where failing loudly is the point.
- **The bind address is `tailscale ip -4` and nothing else.** No fallback bind, no `--host` override, no development mode. Binding is the access control ([ADR 7](../../adr/0007-bind-tailscale-address-only.md)).
- **The handler never collects and never probes.** It reads the last published `Snapshot`.
- **The page is read-only.** No forms, no buttons, no state-changing routes. Only `/` and `/ports.json` exist; everything else is 404.
- **`/ports.json`'s shape is fixed by `src/harbor_console/ports/live.py`**, which already consumes it. `fetch_live` rejects a `port` that is not a real integer. Do not change `live.py` to fit the emitter; change the emitter.
- Run tests with `uv run pytest`. Never `pip`, never `python -m venv`.
- Windows dev box: a Bash tool and a PowerShell tool are both available, but **do not chain shell commands with `&&`** — issue separate calls. Keep files LF.

### Address normalisation (applies to Tasks 2, 3, 5)

A socket bound to IPv6 `::` accepts IPv4 traffic too, so it is the wildcard in
practice. The allocator's `keys.addrs_overlap` treats the literal string
`"0.0.0.0"` as the wildcard and knows nothing about `"::"`. **Normalise `"::"`
to `"0.0.0.0"` at every collection point**, so a dual-stack listener correctly
contends with everything on its host. Leave other IPv6 addresses as they are.

---

### Task 1: `tailnet.py` — the bind address

**Files:**
- Create: `src/harbor_console/tailnet.py`
- Test: `tests/test_tailnet.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `tailnet.TailnetUnavailable(Exception)`
  - `tailnet.tailscale_address(run: Callable[..., object] = subprocess.run) -> str`

This is the only collector in the project permitted to raise. ADR 7 requires the
service to refuse to start rather than bind anything broader.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tailnet.py`:

```python
from types import SimpleNamespace

import pytest

from harbor_console.tailnet import TailnetUnavailable, tailscale_address


def fake_run(stdout="", returncode=0, raises=None):
    def run(*_args, **_kwargs):
        if raises is not None:
            raise raises
        return SimpleNamespace(stdout=stdout, returncode=returncode)

    return run


def test_returns_the_first_address():
    assert tailscale_address(run=fake_run("100.69.239.123\n")) == "100.69.239.123"


def test_ignores_trailing_addresses():
    run = fake_run("100.69.239.123\nfd7a:115c:a1e0::1\n")

    assert tailscale_address(run=run) == "100.69.239.123"


def test_missing_binary_raises():
    with pytest.raises(TailnetUnavailable, match="tailscale"):
        tailscale_address(run=fake_run(raises=FileNotFoundError()))


def test_non_zero_exit_raises():
    with pytest.raises(TailnetUnavailable):
        tailscale_address(run=fake_run(stdout="", returncode=1))


def test_empty_output_raises():
    with pytest.raises(TailnetUnavailable):
        tailscale_address(run=fake_run(stdout="\n"))


def test_unparseable_output_raises():
    with pytest.raises(TailnetUnavailable, match="not an IPv4"):
        tailscale_address(run=fake_run(stdout="something went wrong\n"))


def test_an_ipv6_only_answer_raises():
    with pytest.raises(TailnetUnavailable, match="not an IPv4"):
        tailscale_address(run=fake_run(stdout="fd7a:115c:a1e0::1\n"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_tailnet.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor_console.tailnet'`

- [ ] **Step 3: Write the implementation**

Create `src/harbor_console/tailnet.py`:

```python
"""The address harbor-console-web binds, and the only one it will accept.

Every other collector in this project degrades quietly on a hostile
environment. This one raises, because the page is an inventory of every service
on the host: a silent fallback to a broader address would publish it to the
whole LAN. Binding is the access control, which is why there is no login page.
See ADR 7.
"""

from __future__ import annotations

import ipaddress
import subprocess
from collections.abc import Callable


class TailnetUnavailable(Exception):
    """The host's Tailscale address could not be determined."""


def tailscale_address(run: Callable[..., object] = subprocess.run) -> str:
    """Return the host's Tailscale IPv4 address.

    Asks `tailscale` itself rather than guessing from an interface name or an
    address range, because it is the authority on its own address.
    """
    try:
        result = run(
            ["tailscale", "ip", "-4"],
            check=False,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError) as exc:
        raise TailnetUnavailable(f"could not run tailscale: {exc}") from exc

    if result.returncode != 0:  # type: ignore[attr-defined]
        raise TailnetUnavailable(
            f"tailscale ip -4 exited {result.returncode}"  # type: ignore[attr-defined]
        )

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]  # type: ignore[attr-defined]
    if not lines:
        raise TailnetUnavailable("tailscale ip -4 returned no address")

    candidate = lines[0]
    try:
        ipaddress.IPv4Address(candidate)
    except ValueError as exc:
        raise TailnetUnavailable(f"'{candidate}' is not an IPv4 address") from exc

    return candidate
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_tailnet.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/tailnet.py tests/test_tailnet.py
git commit -m "feat(web): resolve the tailnet bind address, or refuse to start"
```

---

### Task 2: `listening.py` — what is listening on this host

**Files:**
- Create: `src/harbor_console/listening.py`
- Test: `tests/test_listening.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `listening.Listener` frozen dataclass: `addr: str`, `port: int`, `pid: int | None`
  - `listening.listening_sockets(net_connections: Callable[..., object] = psutil.net_connections) -> tuple[Listener, ...]`

Running as the unprivileged `harbor` user, `pid` is `None` for another user's
socket. That costs nothing: container names come from Docker, matched on
`(addr, port)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_listening.py`:

```python
from types import SimpleNamespace

import psutil

from harbor_console.listening import Listener, listening_sockets


def conn(ip, port, status=psutil.CONN_LISTEN, pid=None):
    return SimpleNamespace(laddr=SimpleNamespace(ip=ip, port=port), status=status, pid=pid)


def test_returns_only_listening_sockets():
    conns = [
        conn("0.0.0.0", 8080, pid=10),
        conn("10.0.0.1", 51234, status=psutil.CONN_ESTABLISHED, pid=11),
    ]

    result = listening_sockets(net_connections=lambda kind: conns)

    assert result == (Listener("0.0.0.0", 8080, 10),)


def test_ipv6_wildcard_is_normalised_to_the_ipv4_wildcard():
    result = listening_sockets(net_connections=lambda kind: [conn("::", 22)])

    assert result == (Listener("0.0.0.0", 22, None),)


def test_other_ipv6_addresses_are_left_alone():
    result = listening_sockets(net_connections=lambda kind: [conn("fd7a::1", 8443)])

    assert result[0].addr == "fd7a::1"


def test_a_socket_with_no_local_address_is_skipped():
    conns = [SimpleNamespace(laddr=(), status=psutil.CONN_LISTEN, pid=None)]

    assert listening_sockets(net_connections=lambda kind: conns) == ()


def test_access_denied_degrades_to_empty():
    def denied(kind):
        raise psutil.AccessDenied()

    assert listening_sockets(net_connections=denied) == ()


def test_any_oserror_degrades_to_empty():
    def boom(kind):
        raise OSError("nope")

    assert listening_sockets(net_connections=boom) == ()


def test_results_are_sorted_and_deduplicated():
    conns = [conn("0.0.0.0", 9000), conn("0.0.0.0", 22), conn("0.0.0.0", 22)]

    result = listening_sockets(net_connections=lambda kind: conns)

    assert [item.port for item in result] == [22, 9000]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_listening.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor_console.listening'`

- [ ] **Step 3: Write the implementation**

Create `src/harbor_console/listening.py`:

```python
"""Every socket listening on this host.

Only the host itself can see loopback-bound listeners and non-Docker ones such
as sshd and tailscaled. An allocator blind to those would eventually hand one
out, which is why this is collected here and served to it rather than probed
from outside.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import psutil

#: A socket bound to IPv6 `::` accepts IPv4 traffic too, so it is the wildcard
#: in practice. The allocator's overlap rule knows `0.0.0.0` and nothing else,
#: so normalise here rather than teaching every consumer about both spellings.
IPV6_ANY = "::"
IPV4_ANY = "0.0.0.0"


@dataclass(frozen=True)
class Listener:
    """One listening socket. `pid` is None when it belongs to another user."""

    addr: str
    port: int
    pid: int | None


def listening_sockets(
    net_connections: Callable[..., object] = psutil.net_connections,
) -> tuple[Listener, ...]:
    """Collect listening TCP sockets. Degrades to empty rather than raising."""
    try:
        connections = net_connections(kind="tcp")
    except (psutil.Error, OSError):
        return ()

    found: set[Listener] = set()
    for connection in connections:  # type: ignore[union-attr]
        if connection.status != psutil.CONN_LISTEN:
            continue
        laddr = connection.laddr
        if not laddr:
            continue
        addr = IPV4_ANY if laddr.ip == IPV6_ANY else laddr.ip
        found.add(Listener(addr=addr, port=int(laddr.port), pid=connection.pid))

    return tuple(sorted(found, key=lambda item: (item.port, item.addr)))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_listening.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/listening.py tests/test_listening.py
git commit -m "feat(web): collect listening sockets from psutil"
```

---

### Task 3: `docker.py` — which container publishes which port

**Files:**
- Create: `src/harbor_console/docker.py`
- Test: `tests/test_docker.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `docker.Container` frozen dataclass: `name: str`, `published: tuple[tuple[str, int], ...]`
  - `docker.running_containers(run: Callable[..., object] = subprocess.run) -> tuple[Container, ...]`
  - `docker.DOCKER_UNAVAILABLE: object` — sentinel returned when Docker could not be read at all, so a caller can tell "nothing running" from "could not ask"

Do **not** touch `system.py`'s `get_docker_container_count()`. Rewiring the
shipped tty1 path is not this change's job.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_docker.py`:

```python
from types import SimpleNamespace

from harbor_console.docker import DOCKER_UNAVAILABLE, Container, running_containers


def fake_run(stdout="", returncode=0, raises=None):
    def run(*_args, **_kwargs):
        if raises is not None:
            raise raises
        return SimpleNamespace(stdout=stdout, returncode=returncode)

    return run


def test_parses_names_and_published_ports():
    out = "gte\t0.0.0.0:8080->8080/tcp\narm\t100.69.239.123:49152->8080/tcp\n"

    result = running_containers(run=fake_run(out))

    assert result == (
        Container("arm", (("100.69.239.123", 49152),)),
        Container("gte", (("0.0.0.0", 8080),)),
    )


def test_ipv6_wildcard_publish_is_normalised():
    result = running_containers(run=fake_run("web\t:::8080->8080/tcp\n"))

    assert result[0].published == (("0.0.0.0", 8080),)


def test_a_container_publishing_nothing_still_appears():
    result = running_containers(run=fake_run("shared-postgres\t\n"))

    assert result == (Container("shared-postgres", ()),)


def test_exposed_but_unpublished_ports_are_ignored():
    result = running_containers(run=fake_run("db\t5432/tcp\n"))

    assert result[0].published == ()


def test_several_published_ports_on_one_container():
    out = "app\t0.0.0.0:8501->8501/tcp, 0.0.0.0:8502->8502/tcp\n"

    result = running_containers(run=fake_run(out))

    assert result[0].published == (("0.0.0.0", 8501), ("0.0.0.0", 8502))


def test_missing_binary_reports_unavailable():
    assert running_containers(run=fake_run(raises=FileNotFoundError())) is DOCKER_UNAVAILABLE


def test_non_zero_exit_reports_unavailable():
    assert running_containers(run=fake_run(returncode=1)) is DOCKER_UNAVAILABLE


def test_no_containers_is_empty_not_unavailable():
    result = running_containers(run=fake_run(""))

    assert result == ()
    assert result is not DOCKER_UNAVAILABLE


def test_a_malformed_line_is_skipped_not_fatal():
    result = running_containers(run=fake_run("gte\t0.0.0.0:notaport->8080/tcp\n"))

    assert result == (Container("gte", ()),)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_docker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor_console.docker'`

- [ ] **Step 3: Write the implementation**

Create `src/harbor_console/docker.py`:

```python
"""Live container state, for reconciliation against the lease ledger.

Answers only "which container publishes which host port". Container names are
how a running service is matched to a declared lease; the ledger itself carries
no container field, so the match is by port, with the name used to report a
mismatch when it happens to equal a project's name.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

class _Unavailable(tuple):
    """A distinguishable empty result: falsy, iterable, and identity-checkable."""


#: Returned when Docker could not be asked at all, so a caller can tell that
#: apart from "asked, and nothing is running" -- the difference decides whether
#: the page may claim a service is undeclared. It is an empty tuple subclass, so
#: every consumer can iterate it without caring, while `is DOCKER_UNAVAILABLE`
#: still distinguishes it from an ordinary empty result.
DOCKER_UNAVAILABLE = _Unavailable()

IPV6_ANY = "::"
IPV4_ANY = "0.0.0.0"

#: Matches the published half of `0.0.0.0:8080->8080/tcp` and `:::8080->8080/tcp`.
_PUBLISHED = re.compile(r"^(?P<addr>.*):(?P<port>\d+)->")


@dataclass(frozen=True)
class Container:
    """One running container and the host ports it publishes."""

    name: str
    published: tuple[tuple[str, int], ...]


def running_containers(
    run: Callable[..., object] = subprocess.run,
) -> tuple[Container, ...]:
    """Collect running containers. Returns DOCKER_UNAVAILABLE if Docker cannot be read."""
    try:
        result = run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
            check=False,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return DOCKER_UNAVAILABLE

    if result.returncode != 0:  # type: ignore[attr-defined]
        return DOCKER_UNAVAILABLE

    containers = []
    for line in result.stdout.splitlines():  # type: ignore[attr-defined]
        if not line.strip():
            continue
        name, _, ports = line.partition("\t")
        containers.append(Container(name=name.strip(), published=_publish_pairs(ports)))

    return tuple(sorted(containers, key=lambda item: item.name))


def _publish_pairs(ports: str) -> tuple[tuple[str, int], ...]:
    """Parse the published `addr:port->container/proto` entries from one line."""
    pairs: list[tuple[str, int]] = []
    for entry in ports.split(","):
        entry = entry.strip()
        if "->" not in entry:
            continue
        match = _PUBLISHED.match(entry)
        if match is None:
            continue
        addr = match.group("addr")
        addr = IPV4_ANY if addr in ("", IPV6_ANY, "::") else addr
        pairs.append((addr, int(match.group("port"))))

    return tuple(sorted(set(pairs)))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_docker.py -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/docker.py tests/test_docker.py
git commit -m "feat(web): collect running containers and their published ports"
```

---

### Task 4: `probe.py` — is it up, and what does it say

**Files:**
- Create: `src/harbor_console/probe.py`
- Test: `tests/test_probe.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `probe.Detail` frozen dataclass: `label: str`, `value: str`
  - `probe.Health` frozen dataclass: `up: bool`, `state: str | None`, `summary: str | None`, `detail: tuple[Detail, ...]`, `warning: str | None`
  - `probe.probe(host: str, port: int, opener: Callable[..., object] = urllib.request.urlopen, timeout: float = 2.0) -> Health`

**Any HTTP response means up** — a 404 and a 303 to a login page both prove a
service is answering. `urllib` raises `HTTPError` for 4xx/5xx, and an
`HTTPError` *is* a response, so it must be caught and counted as up.
`/hcstatus` only ever adds detail; it can never make a service DOWN.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_probe.py`:

```python
import json
import urllib.error

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
    routes = {"/": urllib.error.URLError("refused"), "/hcstatus": urllib.error.URLError("x")}

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
    body = json.dumps({"state": "ok", "summary": "x", "detail": ["nope", {"label": "a"}]})
    routes = {"/hcstatus": body.encode(), "/": b""}

    health = probe("h", 1, opener=opener_for(routes))

    assert health.detail == ()
    assert health.warning is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_probe.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor_console.probe'`

- [ ] **Step 3: Write the implementation**

Create `src/harbor_console/probe.py`:

```python
"""Liveness and optional detail for one declared service.

Deliberately dumb: connect, and any HTTP response means up. GTE answers `/`
with a 303 to `/login`; a probe insisting on 200 would call a healthy service
down, and a status page that cries wolf is worse than no status page.

`/hcstatus` only ever adds detail. A project whose endpoint is missing, slow,
malformed or wrongly shaped still shows as up -- with a warning where the
project got it wrong, and silently where it simply does not offer one.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

HCSTATUS_PATH = "/hcstatus"
VALID_STATES = ("ok", "warn", "error")


@dataclass(frozen=True)
class Detail:
    """One label/value row a project chose to publish."""

    label: str
    value: str


@dataclass(frozen=True)
class Health:
    """What one probe learned. `warning` explains an ignored /hcstatus."""

    up: bool
    state: str | None
    summary: str | None
    detail: tuple[Detail, ...]
    warning: str | None


def probe(
    host: str,
    port: int,
    opener: Callable[..., object] = urllib.request.urlopen,
    timeout: float = 2.0,
) -> Health:
    """Probe one service for liveness, then for optional detail."""
    base = f"http://{host}:{port}"

    if not _answers(f"{base}/", opener, timeout):
        return Health(up=False, state=None, summary=None, detail=(), warning=None)

    state, summary, detail, warning = _hcstatus(f"{base}{HCSTATUS_PATH}", opener, timeout)
    return Health(up=True, state=state, summary=summary, detail=detail, warning=warning)


def _answers(url: str, opener: Callable[..., object], timeout: float) -> bool:
    """True when anything answers over HTTP, including an error status."""
    try:
        with opener(url, timeout=timeout):  # type: ignore[union-attr]
            return True
    except urllib.error.HTTPError:
        # A 404 or a 500 is still a service answering.
        return True
    except (OSError, ValueError):
        return False


def _hcstatus(
    url: str, opener: Callable[..., object], timeout: float
) -> tuple[str | None, str | None, tuple[Detail, ...], str | None]:
    """Fetch and validate /hcstatus. Never decides up or down."""
    try:
        with opener(url, timeout=timeout) as response:  # type: ignore[union-attr]
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError:
        # Not offering /hcstatus is the ordinary case, not a fault.
        return None, None, (), None
    except (OSError, ValueError) as exc:
        return None, None, (), f"{HCSTATUS_PATH} unreadable: {exc}"

    if not isinstance(payload, dict):
        return None, None, (), f"{HCSTATUS_PATH} is not a JSON object"

    state = payload.get("state")
    if state is not None and state not in VALID_STATES:
        return None, None, (), f"{HCSTATUS_PATH} state '{state}' is not ok/warn/error"

    summary = payload.get("summary")
    if summary is not None and not isinstance(summary, str):
        return None, None, (), f"{HCSTATUS_PATH} summary is not a string"

    rows = payload.get("detail", [])
    if not isinstance(rows, list):
        return state, summary, (), f"{HCSTATUS_PATH} detail is not a list"

    detail = []
    dropped = False
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("label"), str) and isinstance(
            row.get("value"), (str, int, float)
        ):
            detail.append(Detail(label=row["label"], value=str(row["value"])))
        else:
            dropped = True

    warning = f"{HCSTATUS_PATH} had detail rows that were not label/value" if dropped else None
    return state, summary, tuple(detail), warning
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_probe.py -v`
Expected: PASS, 10 tests

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/probe.py tests/test_probe.py
git commit -m "feat(web): probe liveness dumbly, and /hcstatus optionally"
```

---

### Task 5: `snapshot.py` and `reconcile.py` — the contract and the drift

**Files:**
- Create: `src/harbor_console/snapshot.py`
- Create: `src/harbor_console/reconcile.py`
- Test: `tests/test_reconcile.py`

**Interfaces:**
- Consumes: `listening.Listener`, `docker.Container`, `docker.DOCKER_UNAVAILABLE`, `ports.ledger.Lease`, `ports.keys.addrs_overlap`, `probe.Health`.
- Produces:
  - `snapshot.Drift` frozen dataclass: `kind: str`, `detail: str`
  - `snapshot.Snapshot` frozen dataclass: `collected: datetime`, `metrics: dict[str, str | float | int]`, `leases: tuple[Lease, ...]`, `listeners: tuple[Listener, ...]`, `containers: tuple[Container, ...]`, `docker_available: bool`, `health: dict[tuple[str, str], Health]`, `drift: tuple[Drift, ...]`, `ledger_error: str | None`
  - `reconcile.DECLARED_NOT_RUNNING`, `RUNNING_NOT_DECLARED`, `PORT_MISMATCH` — the three `kind` strings
  - `reconcile.find_drift(leases, listeners, containers, docker_available) -> tuple[Drift, ...]`

`snapshot.py` holds only data, so the prober and the renderer can both import it
without a cycle — the role `ports/keys.py` plays for the allocator.
`reconcile.py` is pure: no I/O, so every rule is testable with plain values.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reconcile.py`:

```python
from datetime import date

from harbor_console.docker import DOCKER_UNAVAILABLE, Container
from harbor_console.listening import Listener
from harbor_console.ports.ledger import Lease
from harbor_console.reconcile import (
    DECLARED_NOT_RUNNING,
    PORT_MISMATCH,
    RUNNING_NOT_DECLARED,
    find_drift,
)

GRANTED = date(2026, 9, 1)


def lease(project, port, addr="0.0.0.0", name="web"):
    return Lease(project, name, "hpz440", addr, port, GRANTED)


def kinds(drift):
    return [item.kind for item in drift]


def test_a_lease_with_nothing_listening_is_declared_not_running():
    drift = find_drift([lease("gte", 8080)], [], [], docker_available=True)

    assert kinds(drift) == [DECLARED_NOT_RUNNING]
    assert "gte" in drift[0].detail


def test_a_lease_with_a_listener_is_no_drift():
    drift = find_drift(
        [lease("gte", 8080)],
        [Listener("0.0.0.0", 8080, None)],
        [Container("gte", (("0.0.0.0", 8080),))],
        docker_available=True,
    )

    assert drift == ()


def test_a_wildcard_listener_satisfies_a_specific_address_lease():
    drift = find_drift(
        [lease("arm", 49152, addr="100.69.239.123")],
        [Listener("0.0.0.0", 49152, None)],
        [Container("arm", (("0.0.0.0", 49152),))],
        docker_available=True,
    )

    assert drift == ()


def test_a_container_publishing_an_unleased_port_is_running_not_declared():
    drift = find_drift(
        [],
        [Listener("0.0.0.0", 9999, None)],
        [Container("stranger", (("0.0.0.0", 9999),))],
        docker_available=True,
    )

    assert kinds(drift) == [RUNNING_NOT_DECLARED]
    assert "stranger" in drift[0].detail


def test_a_container_named_for_a_project_on_the_wrong_port_is_a_mismatch():
    drift = find_drift(
        [lease("gte", 8080)],
        [Listener("0.0.0.0", 9090, None)],
        [Container("gte", (("0.0.0.0", 9090),))],
        docker_available=True,
    )

    assert kinds(drift) == [PORT_MISMATCH]
    assert "8080" in drift[0].detail
    assert "9090" in drift[0].detail


def test_an_unmatched_name_reports_both_halves_instead_of_a_mismatch():
    drift = find_drift(
        [lease("automatic-ripping-machine", 49152)],
        [Listener("0.0.0.0", 49152, None)],
        [Container("arm-rippers-dev", (("0.0.0.0", 49152),))],
        docker_available=True,
    )

    assert kinds(drift) == []


def test_docker_unavailable_suppresses_the_container_side_only():
    drift = find_drift(
        [lease("gte", 8080)],
        [],
        DOCKER_UNAVAILABLE,
        docker_available=False,
    )

    assert kinds(drift) == [DECLARED_NOT_RUNNING]


def test_docker_unavailable_never_claims_a_port_is_undeclared():
    drift = find_drift(
        [],
        [Listener("0.0.0.0", 9999, None)],
        DOCKER_UNAVAILABLE,
        docker_available=False,
    )

    assert drift == ()


def test_a_container_publishing_nothing_is_not_drift():
    drift = find_drift([], [], [Container("shared-postgres", ())], docker_available=True)

    assert drift == ()


def test_findings_are_ordered_deterministically():
    drift = find_drift(
        [lease("zeta", 8001), lease("alpha", 8002)],
        [],
        [],
        docker_available=True,
    )

    assert [item.detail.split()[0] for item in drift] == ["alpha", "zeta"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor_console.reconcile'`

- [ ] **Step 3: Write the implementations**

Create `src/harbor_console/snapshot.py`:

```python
"""The contract between the prober and the renderer.

Data only. The prober publishes one of these on an interval; the handler reads
the last one and renders it. Keeping it in its own module lets both sides
import it without a cycle, the way `ports/keys.py` serves the allocator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from harbor_console.docker import Container
from harbor_console.listening import Listener
from harbor_console.ports.ledger import Lease
from harbor_console.probe import Health


@dataclass(frozen=True)
class Drift:
    """One way the ledger and reality disagree."""

    kind: str
    detail: str


@dataclass(frozen=True)
class Snapshot:
    """Everything the page shows, collected at one moment."""

    collected: datetime
    metrics: dict[str, str | float | int]
    leases: tuple[Lease, ...] = ()
    listeners: tuple[Listener, ...] = ()
    containers: tuple[Container, ...] = ()
    docker_available: bool = True
    health: dict[tuple[str, str], Health] = field(default_factory=dict)
    drift: tuple[Drift, ...] = ()
    ledger_error: str | None = None
```

Create `src/harbor_console/reconcile.py`:

```python
"""Where the ledger and reality disagree.

Pure: leases, listeners and containers in, findings out. No I/O, so every rule
is testable with plain values -- the same reason `ports/allocate.py` is pure.

The join key is `(addr, port)`, compared by overlap, because that is the key the
ledger owns. The ledger carries no container name, so a port mismatch is
reported only when a container's name equals a lease's project name; an
unmatched pair is reported honestly as its two halves instead.
"""

from __future__ import annotations

from collections.abc import Sequence

from harbor_console.docker import Container
from harbor_console.listening import Listener
from harbor_console.ports.keys import addrs_overlap
from harbor_console.ports.ledger import Lease
from harbor_console.snapshot import Drift

DECLARED_NOT_RUNNING = "declared-not-running"
RUNNING_NOT_DECLARED = "running-not-declared"
PORT_MISMATCH = "port-mismatch"


def find_drift(
    leases: Sequence[Lease],
    listeners: Sequence[Listener],
    containers: Sequence[Container],
    docker_available: bool,
) -> tuple[Drift, ...]:
    """Name every disagreement between the ledger and the host."""
    findings: list[Drift] = []
    leased = {(lease.addr, lease.port) for lease in leases}
    mismatched: set[str] = set()

    if docker_available:
        by_name = {container.name: container for container in containers}
        for lease in sorted(leases, key=lambda item: (item.project, item.port)):
            container = by_name.get(lease.project)
            if container is None or not container.published:
                continue
            if not any(
                published_port == lease.port and addrs_overlap(published_addr, lease.addr)
                for published_addr, published_port in container.published
            ):
                actual = ", ".join(f"{a}:{p}" for a, p in container.published)
                findings.append(
                    Drift(
                        PORT_MISMATCH,
                        f"{lease.project} is leased {lease.addr}:{lease.port} "
                        f"but container '{container.name}' publishes {actual}",
                    )
                )
                mismatched.add(lease.project)

    for lease in sorted(leases, key=lambda item: (item.project, item.port)):
        if lease.project in mismatched:
            continue
        if not any(
            listener.port == lease.port and addrs_overlap(listener.addr, lease.addr)
            for listener in listeners
        ):
            findings.append(
                Drift(
                    DECLARED_NOT_RUNNING,
                    f"{lease.project}/{lease.name} leases {lease.addr}:{lease.port}, "
                    "nothing is listening",
                )
            )

    if docker_available:
        for container in sorted(containers, key=lambda item: item.name):
            if container.name in mismatched:
                continue
            for addr, port in container.published:
                if not any(
                    addrs_overlap(addr, leased_addr) and port == leased_port
                    for leased_addr, leased_port in leased
                ):
                    findings.append(
                        Drift(
                            RUNNING_NOT_DECLARED,
                            f"container '{container.name}' publishes {addr}:{port}, "
                            "which no lease covers",
                        )
                    )

    return tuple(findings)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: PASS, 10 tests

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/snapshot.py src/harbor_console/reconcile.py tests/test_reconcile.py
git commit -m "feat(web): the snapshot contract and pure drift reconciliation"
```

---

### Task 6: `web.py` — render the page and serve two routes

**Files:**
- Create: `src/harbor_console/web.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `snapshot.Snapshot`, `snapshot.Drift`, `probe.Health`, `probe.Detail`.
- Produces:
  - `web.ports_payload(snapshot: Snapshot) -> dict`
  - `web.render_page(snapshot: Snapshot) -> bytes`
  - `web.make_handler(get_snapshot: Callable[[], Snapshot]) -> type[BaseHTTPRequestHandler]`

`/ports.json`'s shape is fixed by `ports/live.py`, which already consumes it.
The strongest test of that is a round-trip: build a payload, hand it to the real
`fetch_live`, and assert the listeners survive. That pins the two halves of the
system together.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor_console.web'`

- [ ] **Step 3: Write the implementation**

Create `src/harbor_console/web.py`:

```python
"""Rendering the status page, and serving it.

Renders a snapshot and nothing else -- it never collects and never probes. One
hung service must not make the page slow to load, which is why probing happens
in a background thread and the handler only reads the last published snapshot.

The page is read-only: no forms, no buttons, no state-changing routes. Port
allocation authority was chosen over lifecycle control, so nothing here can
start or stop a container.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from html import escape
from http.server import BaseHTTPRequestHandler

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


def ports_payload(snapshot: Snapshot) -> dict:
    """Build the /ports.json body the allocator reads.

    Container attribution is by (addr, port) against Docker's published ports,
    not by PID: running unprivileged we cannot see another user's process, and
    container processes are never ours.
    """
    owners: dict[tuple[str, int], str] = {}
    for container in snapshot.containers:
        for addr, port in container.published:
            owners[(addr, port)] = container.name

    listening = []
    for listener in snapshot.listeners:
        container = owners.get((listener.addr, listener.port))
        if container is None:
            for (owner_addr, owner_port), name in owners.items():
                if owner_port == listener.port and owner_addr in (listener.addr, "0.0.0.0"):
                    container = name
                    break
        listening.append(
            {"addr": listener.addr, "port": int(listener.port), "container": container}
        )

    return {
        "host": str(snapshot.metrics["hostname"]),
        "collected": snapshot.collected.isoformat(),
        "listening": listening,
    }


def render_page(snapshot: Snapshot) -> bytes:
    """Render the whole status page as one self-contained document."""
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<meta http-equiv='refresh' content='{REFRESH_SECONDS}'>",
        "<title>Harbor Console</title>",
        f"<style>{_STYLE}</style></head><body>",
        f"<h1>{escape(str(snapshot.metrics['hostname']))}</h1>",
    ]

    if snapshot.ledger_error is not None:
        parts.append(
            f"<p class='banner'>The lease ledger could not be reloaded: "
            f"{escape(snapshot.ledger_error)}. Showing the last good directory.</p>"
        )
    if not snapshot.docker_available:
        parts.append(
            "<p class='banner'>Docker could not be read, so undeclared containers "
            "and port mismatches are not reported.</p>"
        )

    parts.append(_host_table(snapshot))
    parts.append(_services_table(snapshot))
    parts.append(_drift_section(snapshot))
    parts.append(
        f"<p class='stamp'>Collected "
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
    cells = "".join(
        f"<tr><td>{escape(label)}</td><td>{escape(str(value))}</td></tr>"
        for label, value in rows
    )
    return f"<h2>Host</h2><table>{cells}</table>"


def _services_table(snapshot: Snapshot) -> str:
    if not snapshot.leases:
        return "<h2>Services</h2><p>No services are declared.</p>"

    rows = []
    for lease in snapshot.leases:
        health = snapshot.health.get((lease.project, lease.name))
        up = health is not None and health.up
        url = f"http://{lease.host}:{lease.port}/"
        status = "UP" if up else "<span class='down'>DOWN</span>"
        summary = escape(health.summary) if health and health.summary else ""
        rows.append(
            f"<tr><td>{escape(lease.project)}/{escape(lease.name)}</td>"
            f"<td><a href='{escape(url)}'>{escape(lease.addr)}:{lease.port}</a></td>"
            f"<td>{status}</td><td>{summary}</td></tr>"
        )
        if health is not None:
            for row in health.detail:
                rows.append(
                    f"<tr class='detail'><td colspan='4'>{escape(row.label)}: "
                    f"{escape(row.value)}</td></tr>"
                )
            if health.warning:
                rows.append(
                    f"<tr class='detail'><td colspan='4'>{escape(health.warning)}</td></tr>"
                )

    return (
        "<h2>Services</h2><table>"
        "<tr><th>Project</th><th>Address</th><th>State</th><th></th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _drift_section(snapshot: Snapshot) -> str:
    if not snapshot.drift:
        return "<h2>Drift</h2><p>No drift: every lease matches what is running.</p>"

    items = "".join(
        f"<li>{escape(item.kind)} &mdash; {escape(item.detail)}</li>" for item in snapshot.drift
    )
    return f"<h2>Drift</h2><ul>{items}</ul>"


def make_handler(get_snapshot: Callable[[], Snapshot]) -> type[BaseHTTPRequestHandler]:
    """Build a handler class that reads the latest snapshot and nothing else."""

    class Handler(BaseHTTPRequestHandler):
        """Serves the page and /ports.json. Read-only; no other route exists."""

        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib's required name
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", render_page(get_snapshot()))
            elif self.path == "/ports.json":
                body = json.dumps(ports_payload(get_snapshot())).encode("utf-8")
                self._send(200, "application/json", body)
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

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web.py -v`
Expected: PASS, 12 tests

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/web.py tests/test_web.py
git commit -m "feat(web): render the status page and serve /ports.json"
```

---

### Task 7: `webapp.py` — the prober thread and the server

**Files:**
- Create: `src/harbor_console/webapp.py`
- Test: `tests/test_webapp.py`

**Interfaces:**
- Consumes: everything from Tasks 1–6, plus `system.collect_system_metrics`, `ports.ledger.load_leases`, `ports.ledger.LedgerError`.
- Produces:
  - `webapp.WEB_PROJECT = "harbor-console"`, `webapp.WEB_PORT_NAME = "web"`
  - `webapp.SnapshotHolder` — `get() -> Snapshot`, `set(snapshot) -> None`
  - `webapp.collect_snapshot(...) -> Snapshot`
  - `webapp.probe_loop(...) -> None`
  - `webapp.own_port(leases) -> int`
  - `webapp.main(argv: list[str] | None = None) -> int`

The prober loop is driven by an injected `sleep`, exactly as `app.run()` is —
tests raise `KeyboardInterrupt` from it to exit after one iteration. The bind is
asserted by injecting a fake server factory and inspecting the address it was
handed; no test opens a port.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_webapp.py`:

```python
from datetime import date, datetime

import pytest

from harbor_console import webapp
from harbor_console.docker import DOCKER_UNAVAILABLE, Container
from harbor_console.listening import Listener
from harbor_console.ports.ledger import Lease, LedgerError
from harbor_console.probe import Health
from harbor_console.snapshot import Snapshot
from harbor_console.tailnet import TailnetUnavailable

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

WEB_LEASE = Lease("harbor-console", "web", "hpz440", "0.0.0.0", 8090, date(2026, 9, 1))
GTE_LEASE = Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 9, 1))


def test_own_port_comes_from_the_ledger():
    assert webapp.own_port([GTE_LEASE, WEB_LEASE]) == 8090


def test_own_port_missing_is_an_error():
    with pytest.raises(webapp.NotDeclared):
        webapp.own_port([GTE_LEASE])


def test_collect_snapshot_gathers_every_source():
    snapshot = webapp.collect_snapshot(
        leases=(GTE_LEASE,),
        now=datetime(2026, 9, 2, 14, 2, 11),
        collector=lambda: METRICS,
        listeners=lambda: (Listener("0.0.0.0", 8080, None),),
        containers=lambda: (Container("gte", (("0.0.0.0", 8080),)),),
        prober=lambda host, port: Health(True, "ok", "fine", (), None),
    )

    assert snapshot.metrics == METRICS
    assert snapshot.docker_available is True
    assert snapshot.health[("gte", "console")].up is True
    assert snapshot.drift == ()
    assert snapshot.ledger_error is None


def test_collect_snapshot_marks_docker_unavailable():
    snapshot = webapp.collect_snapshot(
        leases=(GTE_LEASE,),
        now=datetime(2026, 9, 2, 14, 2, 11),
        collector=lambda: METRICS,
        listeners=lambda: (Listener("0.0.0.0", 8080, None),),
        containers=lambda: DOCKER_UNAVAILABLE,
        prober=lambda host, port: Health(True, None, None, (), None),
    )

    assert snapshot.docker_available is False
    assert snapshot.containers == ()


def test_probe_loop_publishes_a_snapshot_then_exits_cleanly():
    holder = webapp.SnapshotHolder(
        Snapshot(collected=datetime(2026, 1, 1), metrics=METRICS)
    )
    calls = {"count": 0}

    def collect():
        calls["count"] += 1
        return Snapshot(collected=datetime(2026, 9, 2), metrics=METRICS, leases=(GTE_LEASE,))

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    webapp.probe_loop(holder, collect=collect, sleep=fake_sleep, interval=30.0)

    assert calls["count"] == 1
    assert holder.get().leases == (GTE_LEASE,)


def test_probe_loop_keeps_the_last_snapshot_when_collection_fails():
    good = Snapshot(collected=datetime(2026, 1, 1), metrics=METRICS, leases=(GTE_LEASE,))
    holder = webapp.SnapshotHolder(good)

    def collect():
        raise LedgerError("services.toml: boom")

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    webapp.probe_loop(holder, collect=collect, sleep=fake_sleep, interval=30.0)

    assert holder.get().leases == (GTE_LEASE,)
    assert holder.get().ledger_error is not None


def test_main_binds_the_tailnet_address_and_the_leased_port(monkeypatch):
    bound = {}

    class FakeServer:
        def __init__(self, address, handler):
            bound["address"] = address

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            bound["closed"] = True

    monkeypatch.setattr(webapp, "tailscale_address", lambda: "100.69.239.123")
    monkeypatch.setattr(webapp, "load_leases", lambda _path: [WEB_LEASE, GTE_LEASE])

    result = webapp.main(server_factory=FakeServer, start_prober=lambda _holder: None)

    assert result == 0
    assert bound["address"] == ("100.69.239.123", 8090)
    assert bound["closed"] is True


def test_main_refuses_to_start_without_a_tailnet_address(monkeypatch):
    def boom():
        raise TailnetUnavailable("tailscaled is not up")

    monkeypatch.setattr(webapp, "tailscale_address", boom)
    monkeypatch.setattr(webapp, "load_leases", lambda _path: [WEB_LEASE])

    called = {"served": False}

    def factory(_address, _handler):
        called["served"] = True

    result = webapp.main(server_factory=factory, start_prober=lambda _holder: None)

    assert result != 0
    assert called["served"] is False


def test_main_refuses_to_start_when_the_ledger_is_unreadable(monkeypatch):
    monkeypatch.setattr(webapp, "tailscale_address", lambda: "100.69.239.123")

    def boom(_path):
        raise LedgerError("services.toml: unreadable")

    monkeypatch.setattr(webapp, "load_leases", boom)

    result = webapp.main(server_factory=lambda *a: None, start_prober=lambda _h: None)

    assert result != 0


def test_main_refuses_to_start_when_its_own_lease_is_missing(monkeypatch):
    monkeypatch.setattr(webapp, "tailscale_address", lambda: "100.69.239.123")
    monkeypatch.setattr(webapp, "load_leases", lambda _path: [GTE_LEASE])

    result = webapp.main(server_factory=lambda *a: None, start_prober=lambda _h: None)

    assert result != 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_webapp.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor_console.webapp'`

- [ ] **Step 3: Write the implementation**

Create `src/harbor_console/webapp.py`:

```python
"""The harbor-console-web entry point: one prober thread and one HTTP server.

The two processes of this project have independent lifetimes. Logging in at the
attached monitor must not take this page down, and this page must not depend on
anyone being logged in.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

from harbor_console.docker import DOCKER_UNAVAILABLE, Container, running_containers
from harbor_console.listening import Listener, listening_sockets
from harbor_console.ports.ledger import Lease, LedgerError, load_leases
from harbor_console.probe import Health, probe
from harbor_console.reconcile import find_drift
from harbor_console.snapshot import Snapshot
from harbor_console.system import collect_system_metrics
from harbor_console.tailnet import TailnetUnavailable, tailscale_address
from harbor_console.web import make_handler

#: This service is declared in the same ledger it serves, and takes its port
#: from there. Every service is declared, including this one.
WEB_PROJECT = "harbor-console"
WEB_PORT_NAME = "web"

PROBE_INTERVAL_SECONDS = 30.0

LEDGER_PATH = Path(__file__).resolve().parents[2] / "services.toml"


class NotDeclared(Exception):
    """The ledger carries no lease for this service."""


class SnapshotHolder:
    """The one piece of shared state: the last snapshot the prober published."""

    def __init__(self, initial: Snapshot) -> None:
        self._lock = threading.Lock()
        self._snapshot = initial

    def get(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def set(self, snapshot: Snapshot) -> None:
        with self._lock:
            self._snapshot = snapshot


def own_port(leases: Sequence[Lease]) -> int:
    """Find this service's own leased port in the ledger it serves."""
    for lease in leases:
        if lease.project == WEB_PROJECT and lease.name == WEB_PORT_NAME:
            return lease.port
    raise NotDeclared(
        f"no lease for {WEB_PROJECT}/{WEB_PORT_NAME}; this service must be declared"
    )


def collect_snapshot(
    leases: Sequence[Lease],
    now: datetime,
    collector: Callable[[], dict] = collect_system_metrics,
    listeners: Callable[[], tuple[Listener, ...]] = listening_sockets,
    containers: Callable[[], tuple[Container, ...]] = running_containers,
    prober: Callable[[str, int], Health] = probe,
) -> Snapshot:
    """Gather every source once and fold it into one snapshot."""
    found = listeners()
    running = containers()
    docker_available = running is not DOCKER_UNAVAILABLE

    health = {
        (lease.project, lease.name): prober(lease.host, lease.port) for lease in leases
    }

    return Snapshot(
        collected=now,
        metrics=collector(),
        leases=tuple(leases),
        listeners=found,
        containers=tuple(running),
        docker_available=docker_available,
        health=health,
        drift=find_drift(leases, found, running, docker_available),
        ledger_error=None,
    )


def probe_loop(
    holder: SnapshotHolder,
    collect: Callable[[], Snapshot],
    sleep: Callable[[float], None] = time.sleep,
    interval: float = PROBE_INTERVAL_SECONDS,
) -> None:
    """Publish a fresh snapshot on an interval until interrupted.

    A collection failure never takes the page down: the last good snapshot
    stands, with the reason attached, because a page that exists to tell you
    something is wrong must not become the thing that is wrong.
    """
    while True:
        try:
            holder.set(collect())
        except (LedgerError, OSError) as exc:
            holder.set(replace(holder.get(), ledger_error=str(exc)))
        try:
            sleep(interval)
        except KeyboardInterrupt:
            return


def main(
    argv: list[str] | None = None,
    server_factory: Callable[..., object] = ThreadingHTTPServer,
    start_prober: Callable[[SnapshotHolder], None] | None = None,
) -> int:
    """Entry point. Refuses to start rather than binding anything broader."""
    try:
        address = tailscale_address()
    except TailnetUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        leases = load_leases(LEDGER_PATH)
        port = own_port(leases)
    except (LedgerError, NotDeclared) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    holder = SnapshotHolder(
        Snapshot(collected=datetime.now(), metrics=collect_system_metrics(), leases=tuple(leases))
    )

    if start_prober is None:
        start_prober = _default_prober
    start_prober(holder)

    server = server_factory((address, port), make_handler(holder.get))
    try:
        server.serve_forever()  # type: ignore[union-attr]
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()  # type: ignore[union-attr]
    return 0


def _default_prober(holder: SnapshotHolder) -> None:
    """Start the real prober thread against the real collectors."""

    def collect() -> Snapshot:
        leases = load_leases(LEDGER_PATH)
        return collect_snapshot(leases, datetime.now())

    thread = threading.Thread(
        target=probe_loop, args=(holder, collect), name="harbor-prober", daemon=True
    )
    thread.start()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_webapp.py -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest`
Expected: PASS — the 172 pre-existing tests plus everything from Tasks 1–7. `tests/test_app.py` must still pass: bare `harbor-console` still starts the dashboard.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_console/webapp.py tests/test_webapp.py
git commit -m "feat(web): the prober thread and the tailnet-bound server"
```

---

### Task 8: Ship it — entry point, unit, installer, docs

**Files:**
- Modify: `pyproject.toml`
- Create: `deploy/harbor-console-web.service`
- Modify: `deploy/install.sh`
- Modify: `deploy/uninstall.sh`
- Modify: `docs/architecture.md`, `CLAUDE.md`, `founding_document.txt`
- Create: `docs/adr/0012-web-surface-collectors-and-conventions.md`
- Modify: `docs/adr/README.md`

**Interfaces:**
- Consumes: `webapp.main`.
- Produces: the `harbor-console-web` console script.

- [ ] **Step 1: Add the entry point**

In `pyproject.toml`, under `[project.scripts]`, add the second line so it reads:

```toml
[project.scripts]
harbor-console = "harbor_console.app:main"
harbor-console-web = "harbor_console.webapp:main"
```

Verify: `uv sync --extra dev` then `uv run harbor-console-web --help` is not
expected to work (there are no flags); instead confirm the script exists with:

```bash
uv run python -c "from harbor_console.webapp import main; print(callable(main))"
```

Expected: `True`

- [ ] **Step 2: Write the systemd unit**

Create `deploy/harbor-console-web.service`:

```ini
[Unit]
Description=Harbor Console web status page
After=tailscaled.service network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/opt/harbor-console/.venv/bin/harbor-console-web
Restart=always
RestartSec=2
User=harbor
Group=harbor
SupplementaryGroups=docker
NoNewPrivileges=yes
ProtectHome=yes
PrivateTmp=yes
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

`StartLimitIntervalSec=0` is load-bearing, not boilerplate. `Restart=always`
with `RestartSec=2` otherwise trips systemd's default start limit (5 starts in
10 seconds) and gives up permanently — and this service flaps by design while
`tailscaled` acquires an address, which is exactly that scenario.

There is deliberately no `TTYPath` and no `Conflicts=getty@tty1`: this unit must
not touch the console.

- [ ] **Step 3: Install both units**

In `deploy/install.sh`, find the section that installs the single unit and
generalise it to both. The existing script defines `UNIT_NAME` and `UNIT_DEST`;
replace those two variables and every use of them with a loop over both units.
Keep every existing behaviour: the `getty@tty1` masking stays exactly as it is,
and the script stays idempotent.

The installed-and-enabled step must `enable` and then `restart` each unit (not
`enable --now`, which does not restart an already-running unit and so would not
deploy an update).

After the change, confirm the script still contains the tty1 masking line
unchanged:

```bash
grep -n "getty@tty1" deploy/install.sh
```

Expected: the same masking line as before your edit, unmodified.

In `deploy/uninstall.sh`, stop and disable both units and remove both unit
files, keeping the existing `--purge` behaviour and the existing guards.

- [ ] **Step 4: Write ADR 12**

Create `docs/adr/0012-web-surface-collectors-and-conventions.md`:

```markdown
# 12. The web surface collects by convention, not by declaration

Date: 2026-09-02

## Status

Accepted

## Context

`harbor-console-web` must probe each declared service and reconcile the ledger
against Docker. But the server holds only `services.toml`, deployed by
`install.sh`. The projects' `.harbor.toml` files — which carry `container`,
`health_path`, `hcstatus_path` and `description` — live on the dev box and are
never deployed. The prober therefore has a lease and nothing else.

Three ways out were considered: copy the descriptive fields into each lease
record; emit a second generated directory file alongside the ledger; or probe by
convention and reconcile on the key the ledger already owns.

Two facts made the third sufficient. Liveness is deliberately dumb — any HTTP
response means up — so a declared health path barely affects up or down; even a
404 proves a service is answering. And `(host, addr, port)` is already the
ledger's key, so it is enough to join leases against Docker's published ports
without knowing any container's name.

## Decision

We will probe `/` for liveness and `/hcstatus` for optional detail, by
convention, on every leased port. We will reconcile by joining leases against
Docker's published ports on `(addr, port)`, compared by address overlap.

Nothing new is deployed and no descriptive field is copied, so no field can go
stale against the declaration that owns it.

Because the ledger carries no container name, a port mismatch is reported only
when a container's name equals a lease's project name. An unmatched pair is
reported as what it literally is — declared-not-running plus
running-not-declared, the same event seen from both sides.

Sockets are enumerated with `psutil.net_connections`, which is already a
dependency and sees loopback-bound and non-Docker listeners that Docker cannot
report. A socket bound to IPv6 `::` is normalised to `0.0.0.0`, because it
accepts IPv4 traffic and is the wildcard in practice.

## Consequences

- The page needs no deployment artifact beyond the ledger it already has.
- A project cannot declare a custom health path and have the page honour it. If
  one ever genuinely needs to, that is a new decision and a new ADR.
- Name-based mismatch detection is coarse: `arm-rippers-dev` will not be matched
  to `automatic-ripping-machine`, and that pair reports as two findings rather
  than one. Adding a `container` field to the lease would fix it and needs an
  ADR of its own.
- The allocator's fourth drift category — a project's `.env` disagreeing with
  its lease — remains invisible to the page, because it needs files the server
  does not have. `ports scan` reports it; the page cannot and does not pretend
  to.
```

Add to the table in `docs/adr/README.md`:

```markdown
| 0012 | [The web surface collects by convention, not by declaration](0012-web-surface-collectors-and-conventions.md) | Accepted |
```

- [ ] **Step 5: Update the documentation to match what shipped**

In `docs/architecture.md`, move the "Specified, not yet implemented (v0.2.0)"
section into a shipped section listing the real modules: `listening.py`,
`docker.py`, `tailnet.py`, `probe.py`, `reconcile.py`, `snapshot.py`, `web.py`,
`webapp.py` — with their collect / render / coordinate roles, and noting that
`tailnet.py` is the one collector permitted to raise.

In `CLAUDE.md`, do the same in its Architecture section, add
`uv run harbor-console-web` to the Commands table, and update the "What this is"
bullet so `harbor-console-web` is no longer described as "specified but not yet
implemented".

In `founding_document.txt`, update the Repository Layout block to list the
modules that actually exist, and remove any remaining claim that the web surface
is unbuilt.

Check for other stale claims with:

```bash
grep -rn "not yet implemented\|registry.py\|specified but not" --include="*.md" --include="*.txt" .
```

Expected: no hit describes the web surface as unbuilt.

- [ ] **Step 6: Run the full suite and verify the CLI**

```bash
uv run pytest
```

Expected: PASS, all tests.

```bash
uv run harbor-console ports show
```

Expected: the nine leases print, exit 0. Do **not** run `ports sync` or
`ports scan` — they write into sibling repositories.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml deploy docs CLAUDE.md founding_document.txt
git commit -m "feat(web): ship harbor-console-web as a second systemd unit"
```

---

## Not in this plan

- Wiring `system.py`'s container count through the new `docker.py`. The tty1
  path ships and works; rewiring it is not this change's job.
- The three follow-ups left open by the allocator's final review: no
  `scan --new-only` dry run, a stale leftover fence variable dropped without
  being named, and `_current_port` matching a lease without regard to `addr`.
- Any write route, authentication, historical data, or multi-host aggregation.
