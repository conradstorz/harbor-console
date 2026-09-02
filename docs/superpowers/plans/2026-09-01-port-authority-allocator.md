# Port Authority — Allocator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `harbor-console ports` allocator: it reads each project's `.harbor.toml` declaration, leases a free port from a committed ledger, and writes the granted number into that project's `.env` and declaration.

**Architecture:** A pure decision core (`allocate.py`) takes declarations + leases + live host state and returns decisions with no I/O. Thin readers and writers surround it, one file per artifact it touches. The CLI coordinates them and is the only thing that writes. This is the existing collect / render / coordinate split applied to a third surface.

**Tech Stack:** Python 3.13+, `uv`, `pytest`. Standard library only — `tomllib`, `urllib`, `socket`, `dataclasses`, `pathlib`.

Implements `docs/superpowers/specs/2026-09-01-port-authority-design.md`, the allocator half. The observer half (`/ports.json`, the prober, the page, the second systemd unit) is a separate plan; this plan's degraded mode means the allocator is useful before that exists.

## Global Constraints

- **No new runtime dependency.** `tomllib` reads TOML; the standard library cannot write it. The ledger is emitted by a small deterministic writer in `ledger.py`; `.harbor.toml` and `.env` are edited surgically by line so comments and unrelated content survive. Never add `tomli-w`, `PyYAML`, or `requests`.
- **Python 3.13+**, `from __future__ import annotations` at the top of every module, full type hints, module and function docstrings — match `src/harbor_console/system.py`.
- **Tests use `pytest` with `monkeypatch` and `tmp_path`.** No real sockets, no real HTTP, no real clock, no writes outside `tmp_path`. Tests live flat in `tests/`, named `test_ports_<module>.py`.
- **Allocation band is 8100–8999 inclusive.** Never allocate inside 32768–60999 (Linux ephemeral range).
- **The lease key is `(host, addr, port)`.** `0.0.0.0` overlaps every address on that host; two specific addresses never overlap each other.
- **A port is free only when it is neither leased nor listening.** Liveness never revokes a lease.
- **Incumbents never move.** The earlier `granted` date wins; the newcomer is reassigned.
- **Writes are all-or-nothing per project:** ledger, `.harbor.toml`, `.env` and `HARBOR_PORTS.md` for one project land together or not at all.
- **Bare `harbor-console` must keep launching the tty1 dashboard.** The systemd unit runs it with no arguments and must not change behaviour.
- Run tests with `uv run pytest`. Never `pip`, never `python -m venv`.

---

### Task 1: Naming and overlap rules, and the lease ledger

**Files:**
- Create: `src/harbor_console/ports/__init__.py`
- Create: `src/harbor_console/ports/keys.py`
- Create: `src/harbor_console/ports/ledger.py`
- Test: `tests/test_ports_ledger.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `keys.ANY_ADDR: str` (`"0.0.0.0"`), `keys.addrs_overlap(a: str, b: str) -> bool`, `keys.env_var_name(port_name: str) -> str`
  - `ledger.Lease` frozen dataclass with fields `project: str`, `name: str`, `host: str`, `addr: str`, `port: int`, `granted: date`
  - `ledger.LedgerError(Exception)`
  - `ledger.load_leases(path: Path) -> list[Lease]`
  - `ledger.dumps_leases(leases: Sequence[Lease]) -> str`
  - `ledger.save_leases(path: Path, leases: Sequence[Lease]) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ports_ledger.py`:

```python
from datetime import date
from pathlib import Path

import pytest

from harbor_console.ports import keys
from harbor_console.ports.ledger import (
    Lease,
    LedgerError,
    dumps_leases,
    load_leases,
    save_leases,
)


def test_env_var_name_uppercases_and_replaces_punctuation():
    assert keys.env_var_name("dashboard") == "HARBOR_PORT_DASHBOARD"
    assert keys.env_var_name("web-ui.2") == "HARBOR_PORT_WEB_UI_2"


def test_any_addr_overlaps_everything_but_specifics_do_not_collide():
    assert keys.addrs_overlap("0.0.0.0", "100.69.239.123")
    assert keys.addrs_overlap("100.69.239.123", "0.0.0.0")
    assert keys.addrs_overlap("127.0.0.1", "127.0.0.1")
    assert not keys.addrs_overlap("127.0.0.1", "100.69.239.123")


def test_load_missing_file_returns_empty_list(tmp_path: Path):
    assert load_leases(tmp_path / "services.toml") == []


def test_round_trip_preserves_every_field(tmp_path: Path):
    leases = [
        Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5)),
        Lease("arm", "web", "hpz440", "100.69.239.123", 49152, date(2026, 8, 1)),
    ]
    path = tmp_path / "services.toml"
    save_leases(path, leases)

    assert load_leases(path) == leases


def test_dumps_is_deterministic_and_sorted_by_host_port(tmp_path: Path):
    unsorted = [
        Lease("b", "x", "hpz440", "0.0.0.0", 8200, date(2026, 1, 2)),
        Lease("a", "y", "hpz440", "0.0.0.0", 8100, date(2026, 1, 1)),
    ]
    text = dumps_leases(unsorted)

    assert text.index("8100") < text.index("8200")
    assert dumps_leases(unsorted) == text


def test_duplicate_exact_key_is_a_hard_error(tmp_path: Path):
    path = tmp_path / "services.toml"
    path.write_text(
        "[[lease]]\n"
        'project = "gte"\nname = "console"\nhost = "hpz440"\n'
        'addr = "0.0.0.0"\nport = 8080\ngranted = 2026-07-05\n'
        "\n[[lease]]\n"
        'project = "imageharbor"\nname = "dashboard"\nhost = "hpz440"\n'
        'addr = "0.0.0.0"\nport = 8080\ngranted = 2026-08-09\n',
        encoding="utf-8",
    )

    with pytest.raises(LedgerError, match="8080"):
        load_leases(path)


def test_overlapping_addresses_on_one_port_is_a_hard_error(tmp_path: Path):
    path = tmp_path / "services.toml"
    path.write_text(
        "[[lease]]\n"
        'project = "gte"\nname = "console"\nhost = "hpz440"\n'
        'addr = "0.0.0.0"\nport = 8080\ngranted = 2026-07-05\n'
        "\n[[lease]]\n"
        'project = "other"\nname = "web"\nhost = "hpz440"\n'
        'addr = "100.69.239.123"\nport = 8080\ngranted = 2026-08-09\n',
        encoding="utf-8",
    )

    with pytest.raises(LedgerError, match="8080"):
        load_leases(path)


def test_same_port_on_different_hosts_is_fine(tmp_path: Path):
    leases = [
        Lease("a", "web", "hpz440", "0.0.0.0", 8100, date(2026, 1, 1)),
        Lease("b", "web", "other", "0.0.0.0", 8100, date(2026, 1, 1)),
    ]
    path = tmp_path / "services.toml"
    save_leases(path, leases)

    assert len(load_leases(path)) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ports_ledger.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor_console.ports'`

- [ ] **Step 3: Write the implementation**

Create `src/harbor_console/ports/__init__.py`:

```python
"""Port allocation: declarations, leases, and the decisions between them."""

__all__ = ["allocate", "declaration", "keys", "ledger"]
```

Create `src/harbor_console/ports/keys.py`:

```python
"""Pure naming and address-overlap rules shared by the ledger and the allocator.

This module imports nothing from the package so that both the data layer and the
decision layer can depend on it without a cycle.
"""

from __future__ import annotations

import re

ANY_ADDR = "0.0.0.0"

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def addrs_overlap(a: str, b: str) -> bool:
    """Return True when two bind addresses contend for the same port.

    ``0.0.0.0`` claims every address on the host, so it overlaps anything. Two
    different specific addresses can each hold the same port number without
    conflict -- which is how ARM holds 100.69.239.123:49152 without claiming
    49152 from every other project.
    """
    if a == b:
        return True
    return ANY_ADDR in (a, b)


def env_var_name(port_name: str) -> str:
    """Derive the environment variable a declared port is published through."""
    slug = _NON_ALNUM.sub("_", port_name).strip("_").upper()
    return f"HARBOR_PORT_{slug}"
```

Create `src/harbor_console/ports/ledger.py`:

```python
"""The lease ledger: which project holds which (host, addr, port), and since when.

The ledger is the authority on what is *reserved*. Live state is only evidence
about what is *running*; a stopped service keeps its lease.
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from harbor_console.ports.keys import addrs_overlap


class LedgerError(Exception):
    """The ledger is unreadable or self-contradictory."""


@dataclass(frozen=True)
class Lease:
    """One granted port, held by one project until it is released."""

    project: str
    name: str
    host: str
    addr: str
    port: int
    granted: date


_FIELDS = ("project", "name", "host", "addr", "port", "granted")


def load_leases(path: Path) -> list[Lease]:
    """Read and validate the ledger. A missing file is an empty ledger."""
    if not path.exists():
        return []

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise LedgerError(f"{path}: {exc}") from exc

    leases: list[Lease] = []
    for entry in data.get("lease", []):
        missing = [field for field in _FIELDS if field not in entry]
        if missing:
            raise LedgerError(f"{path}: lease missing {', '.join(missing)}")
        leases.append(
            Lease(
                project=entry["project"],
                name=entry["name"],
                host=entry["host"],
                addr=entry["addr"],
                port=int(entry["port"]),
                granted=entry["granted"],
            )
        )

    _reject_overlaps(path, leases)
    return leases


def _reject_overlaps(path: Path, leases: Sequence[Lease]) -> None:
    """A ledger that contradicts itself is an error, not a warning."""
    for i, a in enumerate(leases):
        for b in leases[i + 1 :]:
            if a.host == b.host and a.port == b.port and addrs_overlap(a.addr, b.addr):
                raise LedgerError(
                    f"{path}: {a.host} port {a.port} claimed twice "
                    f"({a.project}/{a.addr} and {b.project}/{b.addr})"
                )


def dumps_leases(leases: Sequence[Lease]) -> str:
    """Emit the ledger as TOML, deterministically ordered.

    Hand-rolled because the standard library reads TOML but cannot write it, and
    this project takes no new runtime dependency. The ledger is entirely
    machine-owned, so there are no comments or formatting to preserve.
    """
    ordered = sorted(leases, key=lambda lease: (lease.host, lease.port, lease.addr))
    blocks = []
    for lease in ordered:
        blocks.append(
            "[[lease]]\n"
            f'project = "{lease.project}"\n'
            f'name    = "{lease.name}"\n'
            f'host    = "{lease.host}"\n'
            f'addr    = "{lease.addr}"\n'
            f"port    = {lease.port}\n"
            f"granted = {lease.granted.isoformat()}\n"
        )
    header = (
        "# Harbor Console port ledger. Written by `harbor-console ports sync`.\n"
        "# The authority on which project holds which (host, addr, port).\n\n"
    )
    return header + "\n".join(blocks)


def save_leases(path: Path, leases: Sequence[Lease]) -> None:
    """Write the ledger, replacing it wholesale."""
    path.write_text(dumps_leases(leases), encoding="utf-8")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ports_ledger.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/ports/__init__.py src/harbor_console/ports/keys.py src/harbor_console/ports/ledger.py tests/test_ports_ledger.py
git commit -m "feat(ports): lease ledger with (host, addr, port) keys"
```

---

### Task 2: Project declarations (`.harbor.toml`)

**Files:**
- Create: `src/harbor_console/ports/declaration.py`
- Test: `tests/test_ports_declaration.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `declaration.PortRequest` frozen dataclass: `name: str`, `want: int | None`, `assigned: int | None`, `addr: str`, `container: str | None`, `health_path: str`, `hcstatus_path: str | None`, `description: str`
  - `declaration.Declaration` frozen dataclass: `project: str`, `host: str`, `path: Path`, `ports: tuple[PortRequest, ...]`
  - `declaration.DeclarationError(Exception)`
  - `declaration.load_declaration(path: Path) -> Declaration`
  - `declaration.write_assigned(path: Path, port_name: str, assigned: int) -> None`

`write_assigned` edits by line rather than re-emitting the file, so the human's comments and `want` value survive untouched.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ports_declaration.py`:

```python
from pathlib import Path

import pytest

from harbor_console.ports.declaration import (
    DeclarationError,
    load_declaration,
    write_assigned,
)

FULL = """\
project = "imageharbor"
host    = "hpz440"

[[port]]
name          = "dashboard"   # the web UI
want          = 8080
container     = "imageharbor"
health_path   = "/"
hcstatus_path = "/hcstatus"
description   = "Photo organiser dashboard"
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".harbor.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_reads_every_field(tmp_path: Path):
    decl = load_declaration(_write(tmp_path, FULL))

    assert decl.project == "imageharbor"
    assert decl.host == "hpz440"
    assert len(decl.ports) == 1
    port = decl.ports[0]
    assert port.name == "dashboard"
    assert port.want == 8080
    assert port.assigned is None
    assert port.addr == "0.0.0.0"
    assert port.container == "imageharbor"
    assert port.health_path == "/"
    assert port.hcstatus_path == "/hcstatus"


def test_optional_fields_default(tmp_path: Path):
    decl = load_declaration(
        _write(tmp_path, 'project = "p"\nhost = "h"\n\n[[port]]\nname = "web"\n')
    )
    port = decl.ports[0]

    assert port.want is None
    assert port.assigned is None
    assert port.addr == "0.0.0.0"
    assert port.container is None
    assert port.health_path == "/"
    assert port.hcstatus_path is None
    assert port.description == ""


def test_declaration_with_no_ports_is_valid(tmp_path: Path):
    decl = load_declaration(_write(tmp_path, 'project = "shared-postgres"\nhost = "hpz440"\n'))

    assert decl.ports == ()


def test_missing_project_is_an_error(tmp_path: Path):
    with pytest.raises(DeclarationError, match="project"):
        load_declaration(_write(tmp_path, 'host = "hpz440"\n'))


def test_duplicate_port_name_is_an_error(tmp_path: Path):
    text = 'project = "p"\nhost = "h"\n\n[[port]]\nname = "web"\n\n[[port]]\nname = "web"\n'
    with pytest.raises(DeclarationError, match="web"):
        load_declaration(_write(tmp_path, text))


def test_write_assigned_adds_the_field_and_keeps_comments(tmp_path: Path):
    path = _write(tmp_path, FULL)

    write_assigned(path, "dashboard", 8090)

    text = path.read_text(encoding="utf-8")
    assert "assigned      = 8090" in text
    assert "# the web UI" in text
    assert "want          = 8080" in text
    assert load_declaration(path).ports[0].assigned == 8090


def test_write_assigned_replaces_an_existing_value(tmp_path: Path):
    path = _write(tmp_path, FULL)
    write_assigned(path, "dashboard", 8090)
    write_assigned(path, "dashboard", 8091)

    text = path.read_text(encoding="utf-8")
    assert text.count("assigned") == 1
    assert load_declaration(path).ports[0].assigned == 8091


def test_write_assigned_targets_the_right_port_block(tmp_path: Path):
    text = (
        'project = "p"\nhost = "h"\n\n[[port]]\nname = "a"\nwant = 1\n'
        '\n[[port]]\nname = "b"\nwant = 2\n'
    )
    path = _write(tmp_path, text)

    write_assigned(path, "b", 8100)

    ports = {p.name: p.assigned for p in load_declaration(path).ports}
    assert ports == {"a": None, "b": 8100}


def test_write_assigned_rejects_an_unknown_port_name(tmp_path: Path):
    path = _write(tmp_path, FULL)

    with pytest.raises(DeclarationError, match="nosuch"):
        write_assigned(path, "nosuch", 8100)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ports_declaration.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor_console.ports.declaration'`

- [ ] **Step 3: Write the implementation**

Create `src/harbor_console/ports/declaration.py`:

```python
"""Reading and updating a project's `.harbor.toml` declaration.

`want` is human-owned and never rewritten here. `assigned` is harbor-owned and
is written back surgically, one line at a time, so that comments and layout in
somebody else's repository survive being edited by this tool.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from harbor_console.ports.keys import ANY_ADDR


class DeclarationError(Exception):
    """A `.harbor.toml` is unreadable or malformed."""


@dataclass(frozen=True)
class PortRequest:
    """One port a project asks for."""

    name: str
    want: int | None
    assigned: int | None
    addr: str
    container: str | None
    health_path: str
    hcstatus_path: str | None
    description: str


@dataclass(frozen=True)
class Declaration:
    """One project's request, as found in its repository."""

    project: str
    host: str
    path: Path
    ports: tuple[PortRequest, ...]


def load_declaration(path: Path) -> Declaration:
    """Read and validate one `.harbor.toml`."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise DeclarationError(f"{path}: {exc}") from exc
    except OSError as exc:
        raise DeclarationError(f"{path}: {exc}") from exc

    for field in ("project", "host"):
        if field not in data:
            raise DeclarationError(f"{path}: missing '{field}'")

    ports: list[PortRequest] = []
    seen: set[str] = set()
    for entry in data.get("port", []):
        if "name" not in entry:
            raise DeclarationError(f"{path}: a [[port]] has no 'name'")
        name = entry["name"]
        if name in seen:
            raise DeclarationError(f"{path}: duplicate port name '{name}'")
        seen.add(name)
        ports.append(
            PortRequest(
                name=name,
                want=entry.get("want"),
                assigned=entry.get("assigned"),
                addr=entry.get("addr", ANY_ADDR),
                container=entry.get("container"),
                health_path=entry.get("health_path", "/"),
                hcstatus_path=entry.get("hcstatus_path"),
                description=entry.get("description", ""),
            )
        )

    return Declaration(
        project=data["project"],
        host=data["host"],
        path=path,
        ports=tuple(ports),
    )


def write_assigned(path: Path, port_name: str, assigned: int) -> None:
    """Set `assigned` inside the named [[port]] block, leaving all else alone."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    blocks = _port_block_bounds(lines)

    for start, end in blocks:
        if _block_name(lines[start:end]) != port_name:
            continue
        _set_field(lines, start, end, assigned)
        path.write_text("".join(lines), encoding="utf-8")
        return

    raise DeclarationError(f"{path}: no [[port]] named '{port_name}'")


def _port_block_bounds(lines: list[str]) -> list[tuple[int, int]]:
    """Return (start, end) line indices for each [[port]] block."""
    starts = [i for i, line in enumerate(lines) if line.strip() == "[[port]]"]
    bounds = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        bounds.append((start, end))
    return bounds


def _block_name(block: list[str]) -> str | None:
    for line in block:
        key, _, value = line.partition("=")
        if key.strip() == "name":
            return value.split("#")[0].strip().strip('"')
    return None


def _set_field(lines: list[str], start: int, end: int, assigned: int) -> None:
    """Replace an existing `assigned` line, or insert one after `name`."""
    for index in range(start, end):
        if lines[index].partition("=")[0].strip() == "assigned":
            lines[index] = f"assigned      = {assigned}\n"
            return

    for index in range(start, end):
        if lines[index].partition("=")[0].strip() == "name":
            lines.insert(index + 1, f"assigned      = {assigned}\n")
            return

    lines.insert(start + 1, f"assigned      = {assigned}\n")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ports_declaration.py -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/ports/declaration.py tests/test_ports_declaration.py
git commit -m "feat(ports): read and surgically update .harbor.toml declarations"
```

---

### Task 3: Live host state (`/ports.json` and the TCP fallback)

**Files:**
- Create: `src/harbor_console/ports/live.py`
- Test: `tests/test_ports_live.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `live.Listener` frozen dataclass: `addr: str`, `port: int`, `container: str | None`
  - `live.LiveState` frozen dataclass: `host: str`, `listeners: tuple[Listener, ...]`, `complete: bool`
  - `live.LiveState.is_listening(addr: str, port: int) -> bool`
  - `live.LiveState.container_on(port: int) -> str | None` (used for grandfathering)
  - `live.LiveUnavailable(Exception)`
  - `live.fetch_live(url: str, timeout: float = 5.0, opener=urllib.request.urlopen) -> LiveState`
  - `live.probe_live(host: str, ports: Iterable[int], connect=socket.create_connection, timeout: float = 0.5) -> LiveState`

`complete=False` marks the TCP fallback, which cannot see loopback-bound listeners and never learns container names. Task 6 refuses to grant on incomplete state.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ports_live.py`:

```python
import json

import pytest

from harbor_console.ports.live import (
    LiveUnavailable,
    fetch_live,
    probe_live,
)

PAYLOAD = {
    "host": "hpz440",
    "collected": "2026-09-01T14:02:11Z",
    "listening": [
        {"addr": "0.0.0.0", "port": 8080, "container": "gte"},
        {"addr": "127.0.0.1", "port": 5432, "container": "shared-postgres"},
        {"addr": "100.69.239.123", "port": 49152, "container": "arm-rippers-dev"},
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ports_live.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor_console.ports.live'`

- [ ] **Step 3: Write the implementation**

Create `src/harbor_console/ports/live.py`:

```python
"""What is actually listening on the target host.

The authoritative source is `/ports.json`, served read-only by
`harbor-console-web` on the host itself, because only the host can see
loopback-bound listeners and non-Docker ones (sshd, tailscaled) -- an allocator
blind to those would eventually hand one out. When that is unreachable we fall
back to probing over the tailnet and mark the result incomplete.
"""

from __future__ import annotations

import json
import socket
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from harbor_console.ports.keys import ANY_ADDR, addrs_overlap


class LiveUnavailable(Exception):
    """Live host state could not be obtained."""


@dataclass(frozen=True)
class Listener:
    """One socket listening on the host. `container` is None for non-Docker."""

    addr: str
    port: int
    container: str | None


@dataclass(frozen=True)
class LiveState:
    """A snapshot of listening sockets. `complete` is False for TCP probing."""

    host: str
    listeners: tuple[Listener, ...]
    complete: bool

    def is_listening(self, addr: str, port: int) -> bool:
        """True when anything on this host contends for (addr, port)."""
        return any(
            listener.port == port and addrs_overlap(listener.addr, addr)
            for listener in self.listeners
        )

    def container_on(self, port: int) -> str | None:
        """The container holding a port, when known."""
        for listener in self.listeners:
            if listener.port == port:
                return listener.container
        return None


def fetch_live(
    url: str,
    timeout: float = 5.0,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> LiveState:
    """Read authoritative host state from harbor-console-web's /ports.json."""
    try:
        with opener(url, timeout=timeout) as response:  # type: ignore[union-attr]
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise LiveUnavailable(f"{url}: {exc}") from exc

    try:
        listeners = tuple(
            Listener(
                addr=entry["addr"],
                port=int(entry["port"]),
                container=entry.get("container"),
            )
            for entry in payload["listening"]
        )
        host = payload["host"]
    except (KeyError, TypeError, ValueError) as exc:
        raise LiveUnavailable(f"{url}: malformed payload ({exc})") from exc

    return LiveState(host=host, listeners=listeners, complete=True)


def probe_live(
    host: str,
    ports: Iterable[int],
    connect: Callable[..., object] = socket.create_connection,
    timeout: float = 0.5,
) -> LiveState:
    """Fallback: TCP-connect to each port. Blind to loopback and to ownership."""
    listeners = []
    for port in ports:
        try:
            sock = connect((host, port), timeout=timeout)
        except OSError:
            continue
        close = getattr(sock, "close", None)
        if close is not None:
            close()
        listeners.append(Listener(addr=ANY_ADDR, port=port, container=None))

    return LiveState(host=host, listeners=tuple(listeners), complete=False)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ports_live.py -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/ports/live.py tests/test_ports_live.py
git commit -m "feat(ports): live host state via /ports.json with a TCP fallback"
```

---

### Task 4: The allocation decision (pure)

**Files:**
- Create: `src/harbor_console/ports/allocate.py`
- Test: `tests/test_ports_allocate.py`

**Interfaces:**
- Consumes: `ledger.Lease`, `declaration.Declaration`/`PortRequest`, `live.LiveState`, `keys.addrs_overlap`.
- Produces:
  - `allocate.BAND_START = 8100`, `allocate.BAND_END = 8999`
  - `allocate.BandExhausted(Exception)`
  - `allocate.Decision` frozen dataclass: `project: str`, `port_name: str`, `action: str` (`"keep"` | `"grant"` | `"reassign"`), `host: str`, `addr: str`, `port: int`, `reason: str`, `incumbent: Lease | None`
  - `allocate.decide(declarations: Sequence[Declaration], leases: Sequence[Lease], live: LiveState, today: date) -> list[Decision]`
  - `allocate.apply_decisions(leases: Sequence[Lease], decisions: Sequence[Decision], today: date) -> list[Lease]`

This module performs no I/O whatsoever. Every allocation rule is decided and tested here.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ports_allocate.py`:

```python
from datetime import date
from pathlib import Path

import pytest

from harbor_console.ports.allocate import (
    BAND_END,
    BAND_START,
    BandExhausted,
    Decision,
    apply_decisions,
    decide,
)
from harbor_console.ports.declaration import Declaration, PortRequest
from harbor_console.ports.ledger import Lease
from harbor_console.ports.live import Listener, LiveState

TODAY = date(2026, 9, 1)


def decl(project, name, want=None, assigned=None, container=None, addr="0.0.0.0"):
    return Declaration(
        project=project,
        host="hpz440",
        path=Path(f"/tree/{project}/.harbor.toml"),
        ports=(
            PortRequest(
                name=name,
                want=want,
                assigned=assigned,
                addr=addr,
                container=container,
                health_path="/",
                hcstatus_path=None,
                description="",
            ),
        ),
    )


def live(*pairs, complete=True):
    return LiveState(
        host="hpz440",
        listeners=tuple(
            Listener(addr=addr, port=port, container=container)
            for addr, port, container in pairs
        ),
        complete=complete,
    )


def test_free_want_is_granted():
    [decision] = decide([decl("p", "web", want=8080)], [], live(), TODAY)

    assert decision.action == "grant"
    assert decision.port == 8080


def test_existing_assignment_held_by_this_project_is_kept():
    leases = [Lease("p", "web", "hpz440", "0.0.0.0", 8090, date(2026, 8, 1))]

    [decision] = decide([decl("p", "web", want=8080, assigned=8090)], leases, live(), TODAY)

    assert decision.action == "keep"
    assert decision.port == 8090


def test_want_held_by_another_project_moves_the_newcomer_into_the_band():
    leases = [Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5))]

    [decision] = decide([decl("imageharbor", "dashboard", want=8080)], leases, live(), TODAY)

    assert decision.action == "grant"
    assert decision.port == BAND_START
    assert decision.incumbent is not None
    assert decision.incumbent.project == "gte"


def test_a_held_lease_is_written_back_when_the_declaration_lacks_it():
    leases = [Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5))]

    [decision] = decide([decl("gte", "console", want=8080)], leases, live(), TODAY)

    assert decision.action == "grant"
    assert decision.port == 8080
    assert decision.reason == "ledger holds"


def test_incumbent_is_never_moved():
    leases = [
        Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5)),
        Lease("imageharbor", "dashboard", "hpz440", "0.0.0.0", 8090, date(2026, 8, 9)),
    ]
    declarations = [
        decl("gte", "console", want=8080, assigned=8080),
        decl("imageharbor", "dashboard", want=8080, assigned=8090),
    ]

    decisions = decide(declarations, leases, live(), TODAY)

    assert [d.action for d in decisions] == ["keep", "keep"]


def test_a_listening_but_unleased_port_is_not_handed_out():
    state = live(("0.0.0.0", 8100, "somebody-else"))

    [decision] = decide([decl("p", "web")], [], state, TODAY)

    assert decision.port == BAND_START + 1


def test_a_leased_but_stopped_port_is_not_reclaimed():
    leases = [Lease("river", "web", "hpz440", "0.0.0.0", 8100, date(2026, 1, 1))]

    [decision] = decide([decl("p", "web")], leases, live(), TODAY)

    assert decision.port == BAND_START + 1


def test_grandfathering_grants_an_out_of_band_port_the_project_already_runs_on():
    state = live(("100.69.239.123", 49152, "arm-rippers-dev"))
    declaration = decl(
        "arm", "web", want=49152, container="arm-rippers-dev", addr="100.69.239.123"
    )

    [decision] = decide([declaration], [], state, TODAY)

    assert decision.action == "grant"
    assert decision.port == 49152
    assert "grandfathered" in decision.reason


def test_a_specific_address_does_not_collide_with_another_specific_address():
    leases = [Lease("arm", "web", "hpz440", "100.69.239.123", 8080, date(2026, 1, 1))]
    declaration = decl("p", "web", want=8080, addr="127.0.0.1")

    [decision] = decide([declaration], leases, live(), TODAY)

    assert decision.port == 8080


def test_a_declaration_with_no_ports_produces_no_decisions():
    empty = Declaration("shared-postgres", "hpz440", Path("/tree/x/.harbor.toml"), ())

    assert decide([empty], [], live(), TODAY) == []


def test_band_exhaustion_raises():
    leases = [
        Lease("p", f"n{port}", "hpz440", "0.0.0.0", port, date(2026, 1, 1))
        for port in range(BAND_START, BAND_END + 1)
    ]

    with pytest.raises(BandExhausted):
        decide([decl("new", "web")], leases, live(), TODAY)


def test_two_new_declarations_do_not_receive_the_same_port():
    decisions = decide([decl("a", "web"), decl("b", "web")], [], live(), TODAY)

    assert [d.port for d in decisions] == [BAND_START, BAND_START + 1]


def test_apply_decisions_records_grants_and_preserves_grant_dates():
    leases = [Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5))]
    decisions = decide([decl("imageharbor", "dashboard", want=8080)], leases, live(), TODAY)

    updated = apply_decisions(leases, decisions, TODAY)

    assert len(updated) == 2
    incumbent = next(lease for lease in updated if lease.project == "gte")
    newcomer = next(lease for lease in updated if lease.project == "imageharbor")
    assert incumbent.granted == date(2026, 7, 5)
    assert newcomer.granted == TODAY
    assert newcomer.port == BAND_START


def test_a_stale_assignment_against_a_held_port_is_reassigned():
    """The declaration claims 8080, but gte holds it and this project has no lease."""
    leases = [Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5))]

    [decision] = decide(
        [decl("imageharbor", "dashboard", want=8080, assigned=8080)], leases, live(), TODAY
    )

    assert decision.action == "reassign"
    assert decision.port == BAND_START
    assert decision.incumbent.project == "gte"


def test_apply_decisions_moves_a_project_off_its_previous_port():
    leases = [
        Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5)),
        Lease("p", "web", "hpz440", "0.0.0.0", 8200, date(2026, 8, 1)),
    ]
    decisions = [
        Decision("p", "web", "reassign", "hpz440", "0.0.0.0", 8300, "moved", None)
    ]

    updated = apply_decisions(leases, decisions, TODAY)

    ports = {(lease.project, lease.port) for lease in updated}
    assert ("p", 8200) not in ports
    assert ("p", 8300) in ports
    assert ("gte", 8080) in ports
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ports_allocate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor_console.ports.allocate'`

- [ ] **Step 3: Write the implementation**

Create `src/harbor_console/ports/allocate.py`:

```python
"""The allocation policy. Pure: declarations and leases in, decisions out.

Keeping every rule here, with no file or socket touching it, is what makes the
policy testable with plain values -- and it is the only module that decides who
gets which port.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date

from harbor_console.ports.declaration import Declaration, PortRequest
from harbor_console.ports.keys import addrs_overlap
from harbor_console.ports.ledger import Lease
from harbor_console.ports.live import LiveState

#: New grants come from here. Deliberately below the Linux ephemeral range
#: (32768-60999) so an allocated port cannot lose a race to an outbound socket.
BAND_START = 8100
BAND_END = 8999


class BandExhausted(Exception):
    """No free port remains in the allocation band."""


@dataclass(frozen=True)
class Decision:
    """What should happen to one declared port."""

    project: str
    port_name: str
    action: str  # "keep" | "grant" | "reassign"
    host: str
    addr: str
    port: int
    reason: str
    incumbent: Lease | None = None


def decide(
    declarations: Sequence[Declaration],
    leases: Sequence[Lease],
    live: LiveState,
    today: date,
) -> list[Decision]:
    """Resolve every declared port against the ledger and live host state."""
    held = list(leases)
    taken: list[tuple[str, str, int]] = []
    decisions: list[Decision] = []

    for declaration in declarations:
        for request in declaration.ports:
            decision = _decide_one(declaration, request, held, taken, live, today)
            decisions.append(decision)
            taken.append((decision.host, decision.addr, decision.port))

    return decisions


def _decide_one(
    declaration: Declaration,
    request: PortRequest,
    leases: Sequence[Lease],
    taken: Sequence[tuple[str, str, int]],
    live: LiveState,
    today: date,
) -> Decision:
    host, addr = declaration.host, request.addr

    def make(action: str, port: int, reason: str, incumbent: Lease | None = None) -> Decision:
        return Decision(
            project=declaration.project,
            port_name=request.name,
            action=action,
            host=host,
            addr=addr,
            port=port,
            reason=reason,
            incumbent=incumbent,
        )

    own = _lease_for(leases, declaration.project, request.name)

    # 1. A lease this project already holds is never disturbed. When the
    #    declaration does not yet record it, the ledger wins and we write it
    #    back -- the ledger is the authority on what is reserved.
    if own is not None:
        if own.port == request.assigned:
            return make("keep", own.port, "already leased")
        return make("grant", own.port, "ledger holds")

    # 2. A free preference is granted as asked.
    if request.want is not None:
        incumbent = _holder(leases, host, addr, request.want, exclude=declaration.project)
        if incumbent is None and _is_free(request.want, host, addr, leases, taken, live):
            return make("grant", request.want, "preference free")

        # 2b. Grandfathering: already running under this project's own container.
        if incumbent is None and _is_own_listener(live, request):
            return make("grant", request.want, "grandfathered: already running")

        if incumbent is not None:
            port = _first_free(host, addr, leases, taken, live)
            action = "reassign" if request.assigned is not None else "grant"
            return make(action, port, f"{request.want} held by {incumbent.project}", incumbent)

    # 3. No preference, or the preference was unavailable: next free in the band.
    port = _first_free(host, addr, leases, taken, live)
    return make("grant", port, "allocated from band")


def _lease_for(leases: Sequence[Lease], project: str, name: str) -> Lease | None:
    for lease in leases:
        if lease.project == project and lease.name == name:
            return lease
    return None


def _holder(
    leases: Sequence[Lease], host: str, addr: str, port: int, exclude: str
) -> Lease | None:
    """The other project leasing a contending (host, addr, port), if any."""
    candidates = [
        lease
        for lease in leases
        if lease.host == host
        and lease.port == port
        and addrs_overlap(lease.addr, addr)
        and lease.project != exclude
    ]
    return min(candidates, key=lambda lease: lease.granted) if candidates else None


def _is_own_listener(live: LiveState, request: PortRequest) -> bool:
    """True when this project's own container already listens on its preference."""
    if request.container is None or request.want is None:
        return False
    return live.container_on(request.want) == request.container


def _is_free(
    port: int,
    host: str,
    addr: str,
    leases: Sequence[Lease],
    taken: Sequence[tuple[str, str, int]],
    live: LiveState,
) -> bool:
    """Free means neither leased, nor listening, nor promised earlier this run."""
    for lease in leases:
        if lease.host == host and lease.port == port and addrs_overlap(lease.addr, addr):
            return False
    for taken_host, taken_addr, taken_port in taken:
        if taken_host == host and taken_port == port and addrs_overlap(taken_addr, addr):
            return False
    return not live.is_listening(addr, port)


def _first_free(
    host: str,
    addr: str,
    leases: Sequence[Lease],
    taken: Sequence[tuple[str, str, int]],
    live: LiveState,
) -> int:
    for port in range(BAND_START, BAND_END + 1):
        if _is_free(port, host, addr, leases, taken, live):
            return port
    raise BandExhausted(f"no free port in {BAND_START}-{BAND_END} on {host}")


def apply_decisions(
    leases: Sequence[Lease],
    decisions: Sequence[Decision],
    today: date,
) -> list[Lease]:
    """Fold decisions into the ledger, preserving the grant date of kept leases."""
    updated = list(leases)

    for decision in decisions:
        existing = _lease_for(updated, decision.project, decision.port_name)
        if existing is not None and decision.action == "keep":
            continue
        if existing is not None:
            updated.remove(existing)
        updated.append(
            Lease(
                project=decision.project,
                name=decision.port_name,
                host=decision.host,
                addr=decision.addr,
                port=decision.port,
                granted=existing.granted if existing and existing.port == decision.port else today,
            )
        )

    return updated
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ports_allocate.py -v`
Expected: PASS, 13 tests

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/ports/allocate.py tests/test_ports_allocate.py
git commit -m "feat(ports): pure allocation policy with incumbent-wins conflicts"
```

---

### Task 5: The `.env` managed fence and the explainer

**Files:**
- Create: `src/harbor_console/ports/envfile.py`
- Create: `src/harbor_console/ports/explainer.py`
- Test: `tests/test_ports_envfile.py`
- Test: `tests/test_ports_explainer.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `envfile.FENCE_START = "# >>> harbor-console (managed) >>>"`, `envfile.FENCE_END = "# <<< harbor-console (managed) <<<"`
  - `envfile.apply_fence(text: str, values: dict[str, str]) -> str`
  - `envfile.write_env(path: Path, values: dict[str, str]) -> None`
  - `explainer.TEMPLATE_VERSION = 1`, `explainer.TEMPLATE: str`
  - `explainer.write_explainer(path: Path) -> bool` (True when it wrote)

`HARBOR_PORTS.md` is byte-identical in every project and carries no numbers, so it cannot go stale. It is rewritten only when its version line is older than `TEMPLATE_VERSION`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ports_envfile.py`:

```python
from pathlib import Path

from harbor_console.ports.envfile import FENCE_END, FENCE_START, apply_fence, write_env


def test_creates_the_fence_when_absent():
    result = apply_fence("DB_PASSWORD=hunter2\n", {"HARBOR_PORT_WEB": "8090"})

    assert "DB_PASSWORD=hunter2" in result
    assert FENCE_START in result
    assert "HARBOR_PORT_WEB=8090" in result
    assert FENCE_END in result


def test_replaces_only_the_fence_contents():
    original = (
        "BEFORE=1\n"
        f"{FENCE_START}\n"
        "HARBOR_PORT_WEB=8080\n"
        f"{FENCE_END}\n"
        "AFTER=2\n"
    )

    result = apply_fence(original, {"HARBOR_PORT_WEB": "8090"})

    assert "BEFORE=1" in result
    assert "AFTER=2" in result
    assert "8080" not in result
    assert "HARBOR_PORT_WEB=8090" in result
    assert result.count(FENCE_START) == 1


def test_secrets_outside_the_fence_survive_byte_for_byte():
    original = f"A=1\n{FENCE_START}\nHARBOR_PORT_WEB=1\n{FENCE_END}\n# trailing comment\n"

    result = apply_fence(original, {"HARBOR_PORT_WEB": "2"})

    assert result.startswith("A=1\n")
    assert result.endswith("# trailing comment\n")


def test_empty_input_produces_just_the_fence():
    result = apply_fence("", {"HARBOR_PORT_WEB": "8090"})

    assert result == f"{FENCE_START}\nHARBOR_PORT_WEB=8090\n{FENCE_END}\n"


def test_write_env_creates_a_missing_file(tmp_path: Path):
    path = tmp_path / ".env"

    write_env(path, {"HARBOR_PORT_WEB": "8090"})

    assert "HARBOR_PORT_WEB=8090" in path.read_text(encoding="utf-8")


def test_write_env_is_idempotent(tmp_path: Path):
    path = tmp_path / ".env"
    write_env(path, {"HARBOR_PORT_WEB": "8090"})
    first = path.read_text(encoding="utf-8")

    write_env(path, {"HARBOR_PORT_WEB": "8090"})

    assert path.read_text(encoding="utf-8") == first
```

Create `tests/test_ports_explainer.py`:

```python
from pathlib import Path

from harbor_console.ports.explainer import TEMPLATE_VERSION, write_explainer


def test_writes_when_missing(tmp_path: Path):
    path = tmp_path / "HARBOR_PORTS.md"

    assert write_explainer(path) is True
    assert f"harbor-console-template-version: {TEMPLATE_VERSION}" in path.read_text(
        encoding="utf-8"
    )


def test_does_not_rewrite_a_current_file(tmp_path: Path):
    path = tmp_path / "HARBOR_PORTS.md"
    write_explainer(path)
    path.write_text(
        path.read_text(encoding="utf-8") + "\nlocal note\n", encoding="utf-8"
    )

    assert write_explainer(path) is False
    assert "local note" in path.read_text(encoding="utf-8")


def test_rewrites_an_older_version(tmp_path: Path):
    path = tmp_path / "HARBOR_PORTS.md"
    path.write_text("harbor-console-template-version: 0\nstale\n", encoding="utf-8")

    assert write_explainer(path) is True
    assert "stale" not in path.read_text(encoding="utf-8")


def test_contains_no_project_specific_numbers(tmp_path: Path):
    path = tmp_path / "HARBOR_PORTS.md"
    write_explainer(path)
    text = path.read_text(encoding="utf-8")

    assert "8090" not in text
    assert "8100-8999" in text
    assert "hcstatus" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ports_envfile.py tests/test_ports_explainer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor_console.ports.envfile'`

- [ ] **Step 3: Write the implementation**

Create `src/harbor_console/ports/envfile.py`:

```python
"""The managed fence in a project's `.env`.

`.env` usually holds secrets and is usually gitignored, so this writer only ever
rewrites the lines between its own markers. Everything outside them is preserved
exactly.
"""

from __future__ import annotations

from pathlib import Path

FENCE_START = "# >>> harbor-console (managed) >>>"
FENCE_END = "# <<< harbor-console (managed) <<<"


def apply_fence(text: str, values: dict[str, str]) -> str:
    """Return `text` with the managed block replaced by `values`."""
    body = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    block = f"{FENCE_START}\n{body}\n{FENCE_END}\n"

    if FENCE_START in text and FENCE_END in text:
        head, _, rest = text.partition(FENCE_START)
        _, _, tail = rest.partition(FENCE_END)
        return head + block + tail.lstrip("\n")

    if not text:
        return block
    if not text.endswith("\n"):
        text += "\n"
    return text + block


def write_env(path: Path, values: dict[str, str]) -> None:
    """Create or update a project's `.env`, preserving everything else in it."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(apply_fence(existing, values), encoding="utf-8")
```

Create `src/harbor_console/ports/explainer.py`:

```python
"""`HARBOR_PORTS.md`: the rules, dropped into every participating project.

Identical everywhere and free of project-specific numbers, so it can never go
stale and never needs regenerating when an assignment changes. It exists so that
a human -- or an agent -- working inside another repository can find out why a
port is fenced into `.env` without ever seeing harbor-console.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATE_VERSION = 1

_VERSION_LINE = re.compile(r"harbor-console-template-version:\s*(\d+)")

TEMPLATE = f"""\
# Harbor Console — port assignment

harbor-console-template-version: {TEMPLATE_VERSION}

This file is placed in every project that participates in port assignment. It is
identical everywhere and contains no numbers. It is written by harbor-console;
edits are overwritten when the template version changes.

## Why this exists

Published host ports are assigned centrally, not chosen per project. Two projects
once claimed the same port. The loser bound nothing, logged it, and kept running
— so the only symptom was a dashboard that never appeared on a service that
otherwise looked healthy. Nothing decided who owned the port, and nothing checked
before the second project claimed it.

## The three files

| File | Holds | Owned by |
| --- | --- | --- |
| `.harbor.toml` (this project) | what this project **wants** | you |
| `.env` (this project) | what it **got** — the effective number | harbor-console |
| `services.toml` (harbor-console) | the **lease** — who holds what, since when | harbor-console |

`.harbor.toml` also carries an `assigned` field. `want` is yours and is never
rewritten; `assigned` is harbor-console's and must not be hand-edited.

## Rules

- **Do not hard-code a published port in compose.** Use the variable with a
  default: `"${{HARBOR_PORT_NAME:-1234}}:1234"`. The default is what lets this
  project start on a machine where harbor-console has never run.
- **Do not edit inside the `# >>> harbor-console (managed) >>>` fence** in
  `.env`. It is rewritten on every sync. Everything outside it is preserved.
- **To change a port**, edit `want` in `.harbor.toml`, then run
  `harbor-console ports sync` from the harbor-console checkout. You may not get
  what you asked for — if another project already holds it, you are moved and
  told so.
- **The incumbent always wins.** A port already leased is never taken from its
  holder, and a running service is never renumbered underneath you.
- **A stopped service keeps its port.** Ports are not reclaimed because nothing
  is listening.
- **New ports come from 8100-8999**, deliberately below the Linux ephemeral range
  (32768-60999) so an assigned port cannot lose a race to an outbound socket.
- **After a reassignment, redeploy.** Until you do, the running container still
  holds the old port and harbor-console will report the mismatch.

## Health and status endpoints

harbor-console probes each declared port to show whether this project is up.

- `health_path` (usually `/`) — **any HTTP response means up**, including a
  redirect to a login page. A probe insisting on 200 would call a healthy service
  down.
- `hcstatus_path` (optional, conventionally `/hcstatus`) — richer detail,
  rendered on the status page. It never affects up/down: if it is missing,
  broken, slow, or malformed, this project still shows as up, with a warning.
  Return:

      {{"state": "ok",
        "summary": "3 queued",
        "detail": [{{"label": "queue", "value": "3"}},
                   {{"label": "last run", "value": "14:02"}}]}}

  `state` is `ok`, `warn`, or `error`. `summary` is one short line. `detail` is a
  list of label/value pairs of your choosing, rendered verbatim.

## If harbor-console is gone

Nothing here breaks. The compose default keeps the project running, the numbers
in `.env` and `.harbor.toml` stay valid, and this file explains the convention
well enough to keep following it by hand.
"""


def write_explainer(path: Path) -> bool:
    """Write the explainer when missing or outdated. Returns True when written."""
    if path.exists():
        match = _VERSION_LINE.search(path.read_text(encoding="utf-8"))
        if match is not None and int(match.group(1)) >= TEMPLATE_VERSION:
            return False

    path.write_text(TEMPLATE, encoding="utf-8")
    return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ports_envfile.py tests/test_ports_explainer.py -v`
Expected: PASS, 10 tests

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/ports/envfile.py src/harbor_console/ports/explainer.py tests/test_ports_envfile.py tests/test_ports_explainer.py
git commit -m "feat(ports): managed .env fence and the HARBOR_PORTS.md explainer"
```

---

### Task 6: Tree discovery and compose default auditing

**Files:**
- Create: `src/harbor_console/ports/discovery.py`
- Create: `src/harbor_console/ports/compose.py`
- Test: `tests/test_ports_discovery.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `discovery.tree_root(env: Mapping[str, str] | None = None, start: Path | None = None) -> Path`
  - `discovery.find_declarations(root: Path) -> list[Path]`
  - `compose.PublishedPort` frozen dataclass: `file: Path`, `var: str | None`, `default: int | None`, `literal: int | None`
  - `compose.published_ports(project_dir: Path) -> list[PublishedPort]`

Participation is opt-in by the presence of `.harbor.toml`, so worktrees, archives and vendored checkouts are excluded by doing nothing.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ports_discovery.py`:

```python
from pathlib import Path

from harbor_console.ports.compose import published_ports
from harbor_console.ports.discovery import find_declarations, tree_root


def test_tree_root_prefers_the_environment_override(tmp_path: Path):
    assert tree_root(env={"HARBOR_TREE_ROOT": str(tmp_path)}) == tmp_path


def test_tree_root_defaults_to_the_parent_of_the_repo(tmp_path: Path):
    repo = tmp_path / "programming" / "harbor-console"
    repo.mkdir(parents=True)

    assert tree_root(env={}, start=repo) == tmp_path / "programming"


def test_find_declarations_scans_direct_children_only(tmp_path: Path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / ".harbor.toml").write_text("", encoding="utf-8")
    (tmp_path / "beta").mkdir()
    nested = tmp_path / "beta" / "deep"
    nested.mkdir()
    (nested / ".harbor.toml").write_text("", encoding="utf-8")

    found = find_declarations(tmp_path)

    assert found == [tmp_path / "alpha" / ".harbor.toml"]


def test_find_declarations_is_sorted_and_tolerates_a_missing_root(tmp_path: Path):
    for name in ("zeta", "alpha"):
        (tmp_path / name).mkdir()
        (tmp_path / name / ".harbor.toml").write_text("", encoding="utf-8")

    assert [path.parent.name for path in find_declarations(tmp_path)] == ["alpha", "zeta"]
    assert find_declarations(tmp_path / "nope") == []


def test_published_ports_reads_variables_defaults_and_literals(tmp_path: Path):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    ports:\n"
        '      - "${HARBOR_PORT_WEB:-8080}:8080"\n'
        '      - "9000:9000"\n'
        '      - "100.69.239.123:49152:8080"\n',
        encoding="utf-8",
    )

    found = published_ports(tmp_path)

    assert (found[0].var, found[0].default) == ("HARBOR_PORT_WEB", 8080)
    assert found[1].literal == 9000
    assert found[2].literal == 49152


def test_published_ports_covers_every_compose_variant(tmp_path: Path):
    (tmp_path / "docker-compose.yml").write_text(
        'services:\n  a:\n    ports:\n      - "1:1"\n', encoding="utf-8"
    )
    (tmp_path / "docker-compose.prod.yml").write_text(
        'services:\n  a:\n    ports:\n      - "2:2"\n', encoding="utf-8"
    )

    names = sorted(port.file.name for port in published_ports(tmp_path))

    assert names == ["docker-compose.prod.yml", "docker-compose.yml"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ports_discovery.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor_console.ports.compose'`

- [ ] **Step 3: Write the implementation**

Create `src/harbor_console/ports/discovery.py`:

```python
"""Finding the projects that participate in port assignment.

harbor-console lives inside the tree it scans, so the root needs no
configuration. Participation is opt-in: a project joins by adding
`.harbor.toml`, which is what keeps worktrees, archives and vendored checkouts
out without an exclusion list to maintain.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

DECLARATION_NAME = ".harbor.toml"


def tree_root(env: Mapping[str, str] | None = None, start: Path | None = None) -> Path:
    """The directory whose children are scanned for declarations."""
    environ = os.environ if env is None else env
    override = environ.get("HARBOR_TREE_ROOT")
    if override:
        return Path(override)

    repo = start if start is not None else Path(__file__).resolve().parents[3]
    return repo.parent


def find_declarations(root: Path) -> list[Path]:
    """Every direct child project's declaration, sorted by directory name."""
    if not root.is_dir():
        return []

    found = [
        child / DECLARATION_NAME
        for child in sorted(root.iterdir(), key=lambda path: path.name)
        if child.is_dir() and (child / DECLARATION_NAME).is_file()
    ]
    return found
```

Create `src/harbor_console/ports/compose.py`:

```python
"""Reading published ports out of a project's compose files.

Regex rather than a YAML parser: this project takes no new runtime dependency,
and the only thing needed is the published-port strings. Used to warn when a
compose default has drifted from the assignment -- `.env` is usually gitignored,
so the default is what a fresh clone actually gets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_VARIABLE = re.compile(r'"\$\{(?P<var>[A-Z0-9_]+)(?::-(?P<default>\d+))?\}:\d+"')
_LITERAL = re.compile(r'"(?:(?P<addr>[\d.]+):)?(?P<host_port>\d+):\d+"')


@dataclass(frozen=True)
class PublishedPort:
    """One published-port entry found in a compose file."""

    file: Path
    var: str | None
    default: int | None
    literal: int | None


def published_ports(project_dir: Path) -> list[PublishedPort]:
    """Every published port declared by every compose variant in a project."""
    found: list[PublishedPort] = []

    for path in sorted(project_dir.glob("docker-compose*.y*ml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("-"):
                continue

            match = _VARIABLE.search(stripped)
            if match is not None:
                default = match.group("default")
                found.append(
                    PublishedPort(
                        file=path,
                        var=match.group("var"),
                        default=int(default) if default else None,
                        literal=None,
                    )
                )
                continue

            match = _LITERAL.search(stripped)
            if match is not None:
                found.append(
                    PublishedPort(
                        file=path,
                        var=None,
                        default=None,
                        literal=int(match.group("host_port")),
                    )
                )

    return found
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ports_discovery.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/ports/discovery.py src/harbor_console/ports/compose.py tests/test_ports_discovery.py
git commit -m "feat(ports): opt-in tree discovery and compose default auditing"
```

---

### Task 7: The `ports` CLI

**Files:**
- Create: `src/harbor_console/ports/cli.py`
- Modify: `src/harbor_console/app.py` (add subcommand dispatch to `main`)
- Modify: `src/harbor_console/ports/__init__.py` (extend `__all__`)
- Test: `tests/test_ports_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 1–6.
- Produces:
  - `cli.PORTS_URL_DEFAULT = "http://hpz440:8090/ports.json"`
  - `cli.run(argv: Sequence[str], root: Path, ledger_path: Path, live: LiveState, today: date, out: TextIO) -> int`
  - `cli.main(argv: Sequence[str]) -> int`

Exit codes: `0` nothing outstanding; `1` changes pending, withheld, or drift found; `2` a hard error (bad ledger, bad declaration, band exhausted).

Modes: `scan` writes nothing. `sync` applies everything. `sync --new-only` applies grants but withholds reassignments — what the scheduled task runs, so a timer can never renumber a project mid-deploy.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ports_cli.py`:

```python
import io
from datetime import date
from pathlib import Path

from harbor_console.ports import cli
from harbor_console.ports.declaration import load_declaration
from harbor_console.ports.ledger import Lease, load_leases, save_leases
from harbor_console.ports.live import Listener, LiveState

TODAY = date(2026, 9, 1)


def make_project(root: Path, name: str, want: int, container: str | None = None) -> Path:
    project = root / name
    project.mkdir()
    body = f'project = "{name}"\nhost = "hpz440"\n\n[[port]]\nname = "web"\nwant = {want}\n'
    if container:
        body += f'container = "{container}"\n'
    (project / ".harbor.toml").write_text(body, encoding="utf-8")
    return project


def live(*pairs, complete=True):
    return LiveState(
        host="hpz440",
        listeners=tuple(Listener(a, p, c) for a, p, c in pairs),
        complete=complete,
    )


def run(argv, root, ledger_path, state=None):
    out = io.StringIO()
    code = cli.run(
        argv,
        root=root,
        ledger_path=ledger_path,
        live=state if state is not None else live(),
        today=TODAY,
        out=out,
    )
    return code, out.getvalue()


def test_scan_reports_a_pending_grant_and_writes_nothing(tmp_path: Path):
    project = make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"

    code, output = run(["scan"], tmp_path, ledger_path)

    assert code == 1
    assert "alpha" in output
    assert "8080" in output
    assert not ledger_path.exists()
    assert not (project / ".env").exists()
    assert load_declaration(project / ".harbor.toml").ports[0].assigned is None


def test_sync_writes_ledger_env_declaration_and_explainer(tmp_path: Path):
    project = make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"

    code, _ = run(["sync"], tmp_path, ledger_path)

    assert code == 0
    assert [lease.port for lease in load_leases(ledger_path)] == [8080]
    assert "HARBOR_PORT_WEB=8080" in (project / ".env").read_text(encoding="utf-8")
    assert load_declaration(project / ".harbor.toml").ports[0].assigned == 8080
    assert (project / "HARBOR_PORTS.md").is_file()


def test_sync_is_idempotent(tmp_path: Path):
    make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"
    run(["sync"], tmp_path, ledger_path)
    first = ledger_path.read_text(encoding="utf-8")

    code, _ = run(["sync"], tmp_path, ledger_path)

    assert code == 0
    assert ledger_path.read_text(encoding="utf-8") == first


def test_incumbent_keeps_the_port_and_the_newcomer_is_moved(tmp_path: Path):
    make_project(tmp_path, "gte", 8080)
    ledger_path = tmp_path / "services.toml"
    save_leases(ledger_path, [Lease("gte", "web", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5))])
    newcomer = make_project(tmp_path, "imageharbor", 8080)

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 0
    assert "gte" in output
    assert load_declaration(newcomer / ".harbor.toml").ports[0].assigned == 8100
    held = {lease.project: lease.port for lease in load_leases(ledger_path)}
    assert held == {"gte": 8080, "imageharbor": 8100}


def test_new_only_grants_new_but_withholds_a_reassignment(tmp_path: Path):
    ledger_path = tmp_path / "services.toml"
    # Only gte holds a lease. imageharbor's declaration still claims 8080, so it
    # must be reassigned -- and an unattended run must refuse to do that.
    save_leases(ledger_path, [Lease("gte", "web", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5))])
    make_project(tmp_path, "gte", 8080)
    moved = make_project(tmp_path, "imageharbor", 8080)
    (moved / ".harbor.toml").write_text(
        'project = "imageharbor"\nhost = "hpz440"\n\n[[port]]\n'
        'name = "web"\nwant = 8080\nassigned = 8080\n',
        encoding="utf-8",
    )
    fresh = make_project(tmp_path, "moonrise", 8500)

    code, output = run(["sync", "--new-only"], tmp_path, ledger_path)

    assert code == 1
    assert load_declaration(fresh / ".harbor.toml").ports[0].assigned == 8500
    assert load_declaration(moved / ".harbor.toml").ports[0].assigned == 8080
    assert "withheld" in output.lower()


def test_degraded_live_state_refuses_to_grant(tmp_path: Path):
    make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"

    code, output = run(["sync"], tmp_path, ledger_path, state=live(complete=False))

    assert code == 1
    assert "incomplete" in output.lower()
    assert not ledger_path.exists()


def test_scan_warns_when_a_compose_default_has_drifted(tmp_path: Path):
    project = make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"
    run(["sync"], tmp_path, ledger_path)
    (project / "docker-compose.yml").write_text(
        'services:\n  a:\n    ports:\n      - "${HARBOR_PORT_WEB:-9999}:80"\n',
        encoding="utf-8",
    )

    code, output = run(["scan"], tmp_path, ledger_path)

    assert code == 1
    assert "9999" in output


def test_show_lists_leases_and_writes_nothing(tmp_path: Path):
    ledger_path = tmp_path / "services.toml"
    save_leases(ledger_path, [Lease("gte", "web", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5))])

    code, output = run(["show"], tmp_path, ledger_path)

    assert code == 0
    assert "gte" in output
    assert "8080" in output


def test_a_broken_declaration_fails_without_writing_anything(tmp_path: Path):
    project = tmp_path / "bad"
    project.mkdir()
    (project / ".harbor.toml").write_text('host = "hpz440"\n', encoding="utf-8")
    ledger_path = tmp_path / "services.toml"

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 2
    assert "project" in output
    assert not ledger_path.exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ports_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor_console.ports.cli'`

- [ ] **Step 3: Write the implementation**

Create `src/harbor_console/ports/cli.py`:

```python
"""`harbor-console ports` -- the only thing in the allocator that writes.

`scan` reports and writes nothing. `sync` applies. `sync --new-only` grants new
requests but withholds anything that would change an existing assignment, which
is what the scheduled task runs: a timer must never renumber a project you are
mid-deploy on.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import TextIO

from harbor_console.ports import compose, discovery, envfile, explainer
from harbor_console.ports.allocate import BandExhausted, Decision, apply_decisions, decide
from harbor_console.ports.declaration import (
    Declaration,
    DeclarationError,
    load_declaration,
    write_assigned,
)
from harbor_console.ports.keys import env_var_name
from harbor_console.ports.ledger import LedgerError, load_leases, save_leases
from harbor_console.ports.live import LiveState, LiveUnavailable, fetch_live

PORTS_URL_DEFAULT = "http://hpz440:8090/ports.json"

EXIT_OK = 0
EXIT_PENDING = 1
EXIT_ERROR = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harbor-console ports")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("scan", help="report pending grants, conflicts and drift")
    sync = subcommands.add_parser("sync", help="apply grants and reassignments")
    sync.add_argument(
        "--new-only",
        action="store_true",
        help="grant new requests only; withhold anything already assigned",
    )
    subcommands.add_parser("show", help="print the current lease table")
    parser.add_argument("--ports-url", default=PORTS_URL_DEFAULT)
    return parser


def run(
    argv: Sequence[str],
    root: Path,
    ledger_path: Path,
    live: LiveState,
    today: date,
    out: TextIO,
) -> int:
    """Execute one command against an explicit tree, ledger and host state."""
    args = _parser().parse_args(list(argv))

    try:
        leases = load_leases(ledger_path)
        declarations = [load_declaration(path) for path in discovery.find_declarations(root)]
    except (LedgerError, DeclarationError) as exc:
        print(f"error: {exc}", file=out)
        return EXIT_ERROR

    if args.command == "show":
        return _show(leases, out)

    try:
        decisions = decide(declarations, leases, live, today)
    except BandExhausted as exc:
        print(f"error: {exc}", file=out)
        return EXIT_ERROR

    changes = [decision for decision in decisions if decision.action != "keep"]
    warnings = _compose_warnings(declarations, decisions)

    if args.command == "scan":
        _report(changes, warnings, out, applied=False)
        return EXIT_PENDING if changes or warnings else EXIT_OK

    if changes and not live.complete:
        print(
            "live host state is incomplete (no /ports.json); refusing to grant a port "
            "that cannot be verified as unheld. Nothing written.",
            file=out,
        )
        return EXIT_PENDING

    withheld = [d for d in changes if args.new_only and d.action == "reassign"]
    applied = [d for d in changes if d not in withheld]

    _write(declarations, decisions, applied, leases, ledger_path, today)
    _report(applied, warnings, out, applied=True)

    for decision in withheld:
        print(
            f"withheld {decision.project}/{decision.port_name}: would move to "
            f"{decision.port} -- run `harbor-console ports sync`",
            file=out,
        )

    return EXIT_PENDING if withheld or warnings else EXIT_OK


def _write(
    declarations: Sequence[Declaration],
    decisions: Sequence[Decision],
    applied: Sequence[Decision],
    leases: Sequence[object],
    ledger_path: Path,
    today: date,
) -> None:
    """Apply accepted decisions: ledger first, then each project's own files."""
    if not applied:
        return

    by_project = {declaration.project: declaration for declaration in declarations}
    save_leases(ledger_path, apply_decisions(leases, applied, today))  # type: ignore[arg-type]

    effective = {decision.project: {} for decision in decisions}
    for decision in decisions:
        if decision.action == "keep" or decision in applied:
            effective[decision.project][env_var_name(decision.port_name)] = str(decision.port)

    for decision in applied:
        declaration = by_project[decision.project]
        project_dir = declaration.path.parent
        write_assigned(declaration.path, decision.port_name, decision.port)
        envfile.write_env(project_dir / ".env", effective[decision.project])
        explainer.write_explainer(project_dir / "HARBOR_PORTS.md")


def _compose_warnings(
    declarations: Sequence[Declaration], decisions: Sequence[Decision]
) -> list[str]:
    """`.env` is usually gitignored, so a stale compose default is what a clone gets."""
    assigned = {(d.project, env_var_name(d.port_name)): d.port for d in decisions}
    warnings = []

    for declaration in declarations:
        for published in compose.published_ports(declaration.path.parent):
            if published.var is None or published.default is None:
                continue
            expected = assigned.get((declaration.project, published.var))
            if expected is not None and published.default != expected:
                warnings.append(
                    f"{declaration.project}: {published.file.name} defaults "
                    f"{published.var} to {published.default}, assigned {expected}"
                )

    return warnings


def _report(
    changes: Sequence[Decision], warnings: Sequence[str], out: TextIO, applied: bool
) -> None:
    verb = "wrote" if applied else "would write"
    for decision in changes:
        line = f"{verb} {decision.project}/{decision.port_name} = {decision.port}"
        if decision.incumbent is not None:
            line += (
                f"  ({decision.reason}, held since {decision.incumbent.granted.isoformat()})"
            )
        else:
            line += f"  ({decision.reason})"
        print(line, file=out)

    for warning in warnings:
        print(f"warning: {warning}", file=out)

    if not changes and not warnings:
        print("up to date", file=out)


def _show(leases: Sequence[object], out: TextIO) -> int:
    for lease in sorted(leases, key=lambda item: (item.host, item.port)):  # type: ignore[attr-defined]
        print(
            f"{lease.host} {lease.addr}:{lease.port}  {lease.project}/{lease.name}"  # type: ignore[attr-defined]
            f"  since {lease.granted.isoformat()}",  # type: ignore[attr-defined]
            file=out,
        )
    return EXIT_OK


def main(argv: Sequence[str]) -> int:
    """Entry point for `harbor-console ports ...`."""
    args, _ = _parser().parse_known_args(list(argv))
    root = discovery.tree_root()
    ledger_path = Path(__file__).resolve().parents[3] / "services.toml"

    try:
        live = fetch_live(args.ports_url)
    except LiveUnavailable as exc:
        print(f"warning: {exc}", file=sys.stdout)
        live = LiveState(host="", listeners=(), complete=False)

    return run(argv, root, ledger_path, live, date.today(), sys.stdout)
```

- [ ] **Step 4: Wire the subcommand into the existing entry point**

Modify `src/harbor_console/app.py` — replace `main` with:

```python
def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Bare invocation runs the tty1 dashboard."""
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "ports":
        from harbor_console.ports.cli import main as ports_main

        return ports_main(args[1:])
    return run()
```

and add `import sys` to the imports at the top.

Modify `src/harbor_console/ports/__init__.py`:

```python
"""Port allocation: declarations, leases, and the decisions between them."""

__all__ = [
    "allocate",
    "cli",
    "compose",
    "declaration",
    "discovery",
    "envfile",
    "explainer",
    "keys",
    "ledger",
    "live",
]
```

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS — the 4 pre-existing tests plus every test from Tasks 1–7. Confirm `tests/test_app.py` still passes: bare `main()` must still start the dashboard.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_console/ports/cli.py src/harbor_console/ports/__init__.py src/harbor_console/app.py tests/test_ports_cli.py
git commit -m "feat(ports): scan/sync/show CLI wired into harbor-console"
```

---

### Task 8: Adopt the running fleet, and document it

**Files:**
- Create: `services.toml`
- Create: `.harbor.toml` (harbor-console's own declaration)
- Modify: `CLAUDE.md` (commands table and architecture)
- Modify: `founding_document.txt` (close the enforcement open question)
- Create: `docs/adr/0008-allocate-ports-rather-than-validate.md`
- Modify: `docs/adr/README.md`

**Interfaces:**
- Consumes: the ledger format from Task 1.
- Produces: the initial ledger and harbor-console's own declaration.

The initial ledger records only what is **actually running** on hpz440, dated with the adoption date. ImageHarbor is deliberately absent: it is not running, so it is not an incumbent, and its first `sync` moves it into the band. That is the collision being resolved by the mechanism rather than by hand.

- [ ] **Step 1: Write the initial ledger**

Create `services.toml`:

```toml
# Harbor Console port ledger. Written by `harbor-console ports sync`.
# The authority on which project holds which (host, addr, port).
#
# Adopted 2026-09-01 from what was actually running on hpz440. Ports outside the
# 8100-8999 allocation band are grandfathered where they stand; nothing running is
# renumbered to tidy the band. ARM's 49152 sits inside the Linux ephemeral range
# (32768-60999) and should move when convenient.

[[lease]]
project = "gte"
name    = "console"
host    = "hpz440"
addr    = "0.0.0.0"
port    = 8080
granted = 2026-09-01

[[lease]]
project = "fastapi-docker"
name    = "api"
host    = "hpz440"
addr    = "0.0.0.0"
port    = 8000
granted = 2026-09-01

[[lease]]
project = "harbor-console"
name    = "web"
host    = "hpz440"
addr    = "0.0.0.0"
port    = 8090
granted = 2026-09-01

[[lease]]
project = "retirement-planning"
name    = "app"
host    = "hpz440"
addr    = "0.0.0.0"
port    = 8501
granted = 2026-09-01

[[lease]]
project = "retirement-planning"
name    = "admin"
host    = "hpz440"
addr    = "0.0.0.0"
port    = 8502
granted = 2026-09-01

[[lease]]
project = "my-river-level"
name    = "web"
host    = "hpz440"
addr    = "0.0.0.0"
port    = 5743
granted = 2026-09-01

[[lease]]
project = "ice-colder"
name    = "web"
host    = "hpz440"
addr    = "0.0.0.0"
port    = 26123
granted = 2026-09-01

[[lease]]
project = "ice-colder"
name    = "mqtt"
host    = "hpz440"
addr    = "0.0.0.0"
port    = 1883
granted = 2026-09-01

[[lease]]
project = "automatic-ripping-machine"
name    = "web"
host    = "hpz440"
addr    = "100.69.239.123"
port    = 49152
granted = 2026-09-01
```

- [ ] **Step 2: Verify the ledger loads and is self-consistent**

Run:

```bash
uv run python -c "from pathlib import Path; from harbor_console.ports.ledger import load_leases; print(len(load_leases(Path('services.toml'))))"
```

Expected: `9`, with no `LedgerError`. Then confirm round-tripping is stable:

```bash
uv run python -c "from pathlib import Path; from harbor_console.ports.ledger import dumps_leases, load_leases; p=Path('services.toml'); print(dumps_leases(load_leases(p)) != '')"
```

Expected: `True`

- [ ] **Step 3: Declare harbor-console's own port**

Create `.harbor.toml`:

```toml
# harbor-console takes its port from the same file it serves.
project = "harbor-console"
host    = "hpz440"

[[port]]
name          = "web"
want          = 8090
assigned      = 8090
container     = "harbor-console-web"
health_path   = "/"
hcstatus_path = "/hcstatus"
description   = "Service directory and host status page"
```

- [ ] **Step 4: Write ADR 8**

Create `docs/adr/0008-allocate-ports-rather-than-validate.md`:

```markdown
# 8. Allocate ports rather than validate them

Date: 2026-09-01

## Status

Accepted

## Context

[ADR 6](0006-service-registry-and-web-status-page.md) added the registry but
deliberately left open what makes it *binding*: a file nobody consults is not an
authority. Three mechanisms were on the table — an advisory check command, the
drift list on the status page as the only enforcement, or generated
configuration each project reads.

Two facts decided it. Ports are claimed on the Windows dev box, where the
project tree and the compose files live, but observed on hpz440, where the
containers run; deploys go out through a remote Docker context. And this
repository has no CI, so "fail it in the pipeline" is not an available venue.
An advisory check is only as good as remembering to run it, and the drift list
alone reports a collision that has already happened.

## Decision

We will make the registry binding by having it **hand out the number**, not
merely judge one.

A project declares what it needs in `.harbor.toml`. `harbor-console ports sync`
allocates a free port from 8100–8999, records a lease in `services.toml`, and
writes the number into the project's own `.env` behind a managed fence. Compose
consumes it as `"${HARBOR_PORT_NAME:-default}:container"`, so the project still
starts when harbor-console has never run — the interpolation default is the
project's own preference.

Conflicts are resolved by lease date: the incumbent keeps the port and the
newcomer is moved. Nothing running is ever renumbered. A port is free only when
it is neither leased nor listening, so a stopped service keeps what it holds.

An unattended run may grant new requests but never change an existing
assignment.

## Consequences

- The registry is consulted by construction, because it is the thing that
  produces the number.
- harbor-console writes into repositories it does not own — `.harbor.toml`,
  `.env` and `HARBOR_PORTS.md`. This is the coupling ADR 6 declined to choose,
  now chosen deliberately. Each participating project also needs a one-line
  compose change.
- Every participating project carries `HARBOR_PORTS.md`, identical everywhere,
  so the rules are legible from inside a repository that has never heard of
  harbor-console.
- The allocator needs authoritative host state and gets it from a read-only
  `/ports.json`. When that is unreachable it refuses to grant rather than
  guessing — harbor-console can fail to allocate, but it cannot allocate wrongly.
- Ports already in use are grandfathered where they stand, so adoption renumbers
  nothing that is running.
```

- [ ] **Step 5: Update the ADR index and the founding document**

In `docs/adr/README.md`, add to the table:

```markdown
| 0008 | [Allocate ports rather than validate them](0008-allocate-ports-rather-than-validate.md) | Accepted |
```

In `founding_document.txt`, replace the first Open Questions entry (the paragraph beginning "How the port authority is enforced.") with:

```
How the port authority is enforced was decided on 2026-09-01: by allocation.
Projects declare what they need in .harbor.toml, harbor-console leases a port and
writes it into the project's .env, and the incumbent always keeps what it holds.
See ADR 8. The remaining open question is below.
```

- [ ] **Step 6: Update CLAUDE.md**

In the Commands table, add:

```markdown
| Report pending port changes | `uv run harbor-console ports scan` |
| Apply port assignments | `uv run harbor-console ports sync` |
```

In the Architecture section, under the planned-modules list, mark the allocator implemented by adding:

```markdown
Implemented for v0.2.0:

- `ports/keys.py`, `ports/ledger.py`, `ports/declaration.py` — **collect** the lease ledger and each project's declaration.
- `ports/allocate.py` — the allocation policy. Pure: no I/O, so every rule is testable with plain values.
- `ports/envfile.py`, `ports/explainer.py`, `ports/compose.py`, `ports/discovery.py` — read and write the artifacts in other repositories.
- `ports/cli.py` — **coordinates**; the only module in the allocator that writes.
```

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS, all tests

- [ ] **Step 8: Commit**

```bash
git add services.toml .harbor.toml docs/adr/0008-allocate-ports-rather-than-validate.md docs/adr/README.md founding_document.txt CLAUDE.md
git commit -m "feat(ports): adopt the running fleet as leases, record ADR 8"
```

---

## Not in this plan

The observer half, which becomes its own plan once this one lands:

- `listening.py` — reading listening sockets on hpz440
- `/ports.json` served by `harbor-console-web` (this plan's `fetch_live` already consumes it; until it exists, the allocator runs degraded and refuses to grant)
- the status page, the background prober, `/hcstatus` rendering, drift categories 1–4
- the second systemd unit and its installer changes

Also excluded, per the spec: lifecycle control, access control, multi-host
allocation, and UDP.

The **Windows Task Scheduler entry** that runs `ports sync --new-only` hourly is
deliberately not a task here. The mode it depends on is built and tested in Task
7; registering a scheduled job on the user's machine is an operational step for
them to take, not something this plan should do unattended.
