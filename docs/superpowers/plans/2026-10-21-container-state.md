# Container State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface a container that Docker knows is restarting or unhealthy: as a finding and row state on the status page for any container, and as a platform check that trips the red banner for containers this repository deploys itself.

**Architecture:** The collector (`docker.py`) keeps four more fields from the `docker inspect` JSON it already fetches. The policy (`directory.py`) turns them into two findings and two row states through one shared helper, `container_finding`. `checks.py` reuses that helper for one new check, `platform-containers`, over containers labelled `harbor.platform=true`. `web.py` colours the two new states red. The verdict file and console need no change.

**Tech Stack:** Python 3.13, stdlib dataclasses, pytest via `uv run pytest`.

Spec: `docs/superpowers/specs/2026-10-21-container-state-design.md`.

## Global Constraints

- No new subprocess, no new dependency. Everything comes from the inspect call `running_containers` already makes.
- Collectors never raise: every new field is guarded on its own and takes its default when missing or malformed.
- Findings and the check are withheld (`unknown`, or absent) when Docker is unavailable. Absence of evidence is never a finding.
- `RestartCount` alone is never a finding; it is detail text only.
- Finding kinds: `crash-looping`, `unhealthy`. Row states: `RESTARTING`, `UNHEALTHY`. Check name: `platform-containers`. Label: `harbor.platform: "true"`.
- Detail text, exactly: `container '<name>' is restarting (<n> restarts, last exit <code>)`, or `container '<name>' is restarting (<n> restarts)` when the exit code is unknown; `container '<name>' fails its own healthcheck`.
- Run the whole suite with `uv run pytest` before every commit; it must stay green (663 tests before this plan).
- Commit messages end with the trailer line `Assisted-by: Claude Fable 5.1 (Claude Code)`. Do not add a `Co-Authored-By` line.
- Files in this repo are LF. On Windows, write files with `newline="\n"` or check `git ls-files --eol <file>` shows `w/lf` before committing.

---

### Task 1: The collector keeps container state

**Files:**
- Modify: `src/harbor_console/docker.py` (the `Container` dataclass near line 40 and `_parse` near line 94)
- Test: `tests/test_docker.py`

**Interfaces:**
- Produces: `Container` gains keyword fields `status: str = "running"`, `restarts: int = 0`, `exit_code: int | None = None`, `health: str | None = None`. Every existing positional constructor call stays valid.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_docker.py`:

```python
def test_parses_state_restart_count_exit_code_and_health():
    entry = inspect_entry("watchtower", labels={"harbor.kind": "internal"})
    entry["State"] = {"Status": "restarting", "ExitCode": 1, "Health": {"Status": "unhealthy"}}
    entry["RestartCount"] = 637

    (container,) = running_containers(run=fake_run("abc\n", [entry]))

    assert container.status == "restarting"
    assert container.restarts == 637
    assert container.exit_code == 1
    assert container.health == "unhealthy"


def test_state_fields_default_when_the_entry_has_no_state():
    (container,) = running_containers(run=fake_run("abc\n", [inspect_entry("gte-db-1")]))

    assert (container.status, container.restarts, container.exit_code, container.health) == (
        "running", 0, None, None,
    )


def test_state_fields_default_when_malformed():
    entry = inspect_entry("gte-db-1")
    entry["State"] = {"Status": 7, "ExitCode": "boom", "Health": "nope"}
    entry["RestartCount"] = "many"

    (container,) = running_containers(run=fake_run("abc\n", [entry]))

    assert (container.status, container.restarts, container.exit_code, container.health) == (
        "running", 0, None, None,
    )


def test_health_is_none_when_the_image_has_no_healthcheck():
    entry = inspect_entry("traefik")
    entry["State"] = {"Status": "running", "ExitCode": 0}

    (container,) = running_containers(run=fake_run("abc\n", [entry]))

    assert container.health is None
    assert container.exit_code == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_docker.py -q`
Expected: 4 failures, `AttributeError: 'Container' object has no attribute 'status'`.

- [ ] **Step 3: Add the fields and parse them**

In `src/harbor_console/docker.py`, replace the `Container` dataclass with:

```python
@dataclass(frozen=True)
class Container:
    """One running container: what it publishes, what it declares, how it is doing.

    `status` is Docker's `State.Status` (`running`, `restarting`, ...);
    `restarts` is the cumulative `RestartCount` since the container was
    created; `exit_code` is the last exit; `health` is the healthcheck's
    verdict (`healthy`, `unhealthy`, `starting`) or None when the image has
    none. Defaults describe a container nothing is wrong with, so a caller
    that does not care about state can leave them out.
    """

    name: str
    published: tuple[tuple[str, int], ...]
    labels: dict[str, str] = field(default_factory=dict)
    networks: frozenset[str] = frozenset()
    status: str = "running"
    restarts: int = 0
    exit_code: int | None = None
    health: str | None = None
```

In `_parse`, after the `networks = ...` line and before `return Container(`, add:

```python
    state = entry.get("State") if isinstance(entry.get("State"), dict) else {}
    health = state.get("Health") if isinstance(state.get("Health"), dict) else {}
```

and extend the `return Container(` call with:

```python
        status=_text(state.get("Status"), "running"),
        restarts=_count(entry.get("RestartCount")),
        exit_code=_int_or_none(state.get("ExitCode")),
        health=_text(health.get("Status"), None),
```

Add these helpers after `_parse` (before `_publish_pairs`):

```python
def _text(value: object, default: str | None) -> str | None:
    """A non-empty string, or the default."""
    return value if isinstance(value, str) and value else default


def _int_or_none(value: object) -> int | None:
    """An int, or None. `bool` is an int in Python and is not a count here."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _count(value: object) -> int:
    parsed = _int_or_none(value)
    return parsed if parsed is not None and parsed >= 0 else 0
```

Note `_text(..., "running")` returns `str`; the type checker may want `# type: ignore[arg-type]` on the `status=` line or a cast. Prefer `status=_text(state.get("Status"), "running") or "running"` to keep it a plain `str`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_docker.py -q`
Expected: all pass.

Run: `uv run pytest -q`
Expected: all pass (the fields have defaults, so nothing else moves).

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/docker.py tests/test_docker.py
git commit -F - <<'EOF'
feat: the Docker collector keeps each container's status, restart count, exit code and health

Read from the inspect JSON it already fetched; no new call. Every field is
guarded on its own and defaults to a container nothing is wrong with.

Assisted-by: Claude Fable 5.1 (Claude Code)
EOF
```

---

### Task 2: Findings and row states for a restarting or unhealthy container

**Files:**
- Modify: `src/harbor_console/directory.py` (state constants near line 42, `build_rows` near line 110, finding constants near line 242, `find_findings` near line 262)
- Test: `tests/test_directory.py`

**Interfaces:**
- Consumes: `Container.status`, `Container.restarts`, `Container.exit_code`, `Container.health` from Task 1.
- Produces: `STATE_RESTARTING = "RESTARTING"`, `STATE_UNHEALTHY = "UNHEALTHY"`, `CRASH_LOOPING = "crash-looping"`, `UNHEALTHY = "unhealthy"`, `STATUS_RESTARTING = "restarting"`, `HEALTH_UNHEALTHY = "unhealthy"`, and `container_finding(container: Container) -> Finding | None`, which Task 3 reuses.

- [ ] **Step 1: Write the failing tests**

In `tests/test_directory.py`, add to the `from harbor_console.directory import (...)` block: `CRASH_LOOPING`, `STATE_RESTARTING`, `STATE_UNHEALTHY`, `UNHEALTHY`, `container_finding`. Add `from dataclasses import replace` at the top. Then append:

```python
LOOPING = Container(
    "watchtower", (), {"harbor.kind": "internal"}, frozenset(),
    status="restarting", restarts=637, exit_code=1,
)


def test_a_restarting_container_is_a_crash_looping_finding():
    assert container_finding(LOOPING) == Finding(
        CRASH_LOOPING, "container 'watchtower' is restarting (637 restarts, last exit 1)"
    )


def test_crash_looping_detail_stops_after_the_count_without_an_exit_code():
    assert container_finding(replace(LOOPING, exit_code=None)) == Finding(
        CRASH_LOOPING, "container 'watchtower' is restarting (637 restarts)"
    )


def test_an_unhealthy_container_is_an_unhealthy_finding():
    sick = Container("gte-db-1", (), {"harbor.kind": "internal"}, frozenset(), health="unhealthy")

    assert container_finding(sick) == Finding(
        UNHEALTHY, "container 'gte-db-1' fails its own healthcheck"
    )


def test_a_well_container_has_no_state_finding():
    for health in (None, "healthy", "starting"):
        well = Container("gte-db-1", (), {"harbor.kind": "internal"}, frozenset(), health=health)
        assert container_finding(well) is None


def test_restarting_wins_over_unhealthy_in_the_finding():
    both = replace(LOOPING, health="unhealthy")

    assert container_finding(both).kind == CRASH_LOOPING


def test_find_findings_reports_state_findings_for_declared_and_undeclared_containers():
    undeclared = Container("stray", (), {}, frozenset(), status="restarting", restarts=2, exit_code=137)

    findings = find_findings((LOOPING, undeclared), (), (), None)

    kinds = [(f.kind, f.detail) for f in findings if f.kind == CRASH_LOOPING]
    assert kinds == [
        (CRASH_LOOPING, "container 'stray' is restarting (2 restarts, last exit 137)"),
        (CRASH_LOOPING, "container 'watchtower' is restarting (637 restarts, last exit 1)"),
    ]


def test_state_findings_are_withheld_without_docker():
    assert find_findings(DOCKER_UNAVAILABLE, (), (), None) == ()


def test_a_restarting_internal_container_row_is_restarting():
    rows = build_rows((LOOPING,), (), (), {}, probed=True)

    assert rows == (Row("watchtower", KIND_INTERNAL, "", "watchtower", "", STATE_RESTARTING),)


def test_a_restarting_http_container_row_is_restarting_even_when_traefik_reports_it_live():
    looping = replace(http_container(), status="restarting", restarts=5, exit_code=1)
    router = Router("parksmart@docker", HOST, "parksmart", True, None)

    rows = build_rows((looping,), (router,), (), {"parksmart": UP}, probed=True)

    assert rows[0].state == STATE_RESTARTING


def test_an_unhealthy_tcp_container_row_is_unhealthy():
    mqtt = Container(
        "ice-colder-mqtt", (("0.0.0.0", 1883),), {"harbor.kind": "tcp", "harbor.port": "1883"},
        frozenset(), health="unhealthy",
    )

    rows = build_rows((mqtt,), (), (Listener("0.0.0.0", 1883, None),), {}, probed=True)

    assert rows[0].state == STATE_UNHEALTHY


def test_restarting_beats_unhealthy_in_the_row():
    both = replace(LOOPING, health="unhealthy")

    rows = build_rows((both,), (), (), {}, probed=True)

    assert rows[0].state == STATE_RESTARTING


def test_starting_health_changes_no_row():
    warming = Container("gte-db-1", (), {"harbor.kind": "internal"}, frozenset(), health="starting")

    rows = build_rows((warming,), (), (), {}, probed=True)

    assert rows[0].state == "INTERNAL"
```

Check first how `Router` is constructed elsewhere in this test file (`grep -n "Router(" tests/test_directory.py | head -3`) and match that shape; the five-argument form `Router(name, host, service, enabled, error)` is what `tests/test_checks.py` uses.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_directory.py -q`
Expected: `ImportError: cannot import name 'CRASH_LOOPING'`.

- [ ] **Step 3: Implement**

In `src/harbor_console/directory.py`:

Change the dataclasses import to `from dataclasses import dataclass, replace`.

After `STATE_UNKNOWN = "UNKNOWN"` add:

```python
STATE_RESTARTING = "RESTARTING"
STATE_UNHEALTHY = "UNHEALTHY"

#: Docker's own words for the two conditions `container_finding` reports.
STATUS_RESTARTING = "restarting"
HEALTH_UNHEALTHY = "unhealthy"
```

After `UNDECLARED_TAILNET_LISTENER = "undeclared-tailnet-listener"` add:

```python
CRASH_LOOPING = "crash-looping"
UNHEALTHY = "unhealthy"
```

After the `Finding` dataclass add:

```python
def container_finding(container: Container) -> Finding | None:
    """What Docker itself knows is wrong with this container, or None.

    A restarting container is a crash loop: `restart: always` turning every
    exit into another start, which is how watchtower ran 637 times without a
    row or finding to show for it. Restarting wins over unhealthy because a
    looping container's healthcheck means nothing. The restart count is
    detail, never the trigger: it is cumulative since the container was
    created, and one restart a week ago is not a finding.
    """
    if container.status == STATUS_RESTARTING:
        last = f", last exit {container.exit_code}" if container.exit_code is not None else ""
        return Finding(
            CRASH_LOOPING,
            f"container '{container.name}' is restarting ({container.restarts} restarts{last})",
        )
    if container.health == HEALTH_UNHEALTHY:
        return Finding(UNHEALTHY, f"container '{container.name}' fails its own healthcheck")
    return None


def _with_container_state(row: Row, container: Container) -> Row:
    """Docker's verdict on the container overrides the kind's verdict on the service."""
    finding = container_finding(container)
    if finding is None:
        return row
    state = STATE_RESTARTING if finding.kind == CRASH_LOOPING else STATE_UNHEALTHY
    return replace(row, state=state)
```

Rewrite the body of the `for container in containers:` loop in `build_rows` so every branch produces `row` and one append applies the override:

```python
    for container in containers:
        kind = declared_kind(container)
        if kind is None:
            continue
        description = container.labels.get(LABEL_DESCRIPTION, "")
        if kind == KIND_HTTP:
            row = _http_row(container, by_name, traefik_available, health, probed, description)
        elif kind == KIND_TCP:
            row = _tcp_row(container, listeners, description)
        elif kind == KIND_EDGE:
            row = _edge_row(container, listeners, description)
        else:
            row = Row(container.name, KIND_INTERNAL, "", container.name, description, STATE_INTERNAL)
        rows.append(_with_container_state(row, container))
```

(`container_finding` and `_with_container_state` are defined after `build_rows` in the file; that is fine at call time, but if you prefer, place them above `build_rows`. Keep `Finding` defined before `container_finding`.)

In `find_findings`, inside `if docker_available:`, after the undeclared-container loop and before the bypasses-proxy loop, add:

```python
        for container in sorted(containers, key=lambda c: c.name):
            finding = container_finding(container)
            if finding is not None:
                findings.append(finding)
```

Update the module docstring's list of findings if it enumerates them (it says "Four findings" in `CLAUDE.md`, handled in Task 6; the docstring here only describes labels, so no change unless you find a count).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_directory.py -q`
Expected: all pass.

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/directory.py tests/test_directory.py
git commit -F - <<'EOF'
feat: a restarting or unhealthy container is a finding and overrides its row state

crash-looping and unhealthy, for any container Docker lists, withheld when
Docker is unavailable. RESTARTING and UNHEALTHY replace the state the kind
would have given; restarting wins. The restart count is detail only.

Assisted-by: Claude Fable 5.1 (Claude Code)
EOF
```

---

### Task 3: The `platform-containers` check

**Files:**
- Modify: `src/harbor_console/checks.py` (check names near line 37, `run_checks` near line 104, add `_platform_containers` after `_docker`)
- Test: `tests/test_checks.py`

**Interfaces:**
- Consumes: `container_finding` from Task 2.
- Produces: `CHECK_PLATFORM_CONTAINERS = "platform-containers"`, `LABEL_PLATFORM = "harbor.platform"`. `run_checks` now returns seven checks; the new one is last.

- [ ] **Step 1: Write the failing tests**

In `tests/test_checks.py`, add `CHECK_PLATFORM_CONTAINERS` to the second `from harbor_console.checks import (...)` block (the one near line 71 that imports `CHECK_DOCKER` and friends). Change `test_run_checks_reports_six_checks_in_a_fixed_order` to:

```python
def test_run_checks_reports_seven_checks_in_a_fixed_order():
    names = [check.name for check in run_checks(
        now=NOW, docker_available=True, traefik_available=True, containers=(),
        routers=(), own_route=None, certificate=None, listeners=(),
        listeners_available=True, tailnet_address=None,
    )]

    assert names == [
        CHECK_DOCKER, CHECK_TRAEFIK_API, CHECK_DOCKER_PROVIDER,
        CHECK_OWN_ROUTE, CHECK_CERTIFICATE, CHECK_EDGE_LISTENING,
        CHECK_PLATFORM_CONTAINERS,
    ]
```

Append:

```python
PLATFORM = Container(
    "watchtower", (), {"harbor.kind": "internal", "harbor.platform": "true"}, frozenset()
)


def test_platform_containers_passes_when_every_labelled_container_is_well():
    check = checks(containers=(ROUTED, PLATFORM))[CHECK_PLATFORM_CONTAINERS]

    assert check.state == STATE_OK
    assert check.reason == "1 platform container running"


def test_platform_containers_passes_when_nothing_declares_the_label():
    check = checks(containers=(ROUTED, INTERNAL))[CHECK_PLATFORM_CONTAINERS]

    assert check.state == STATE_OK
    assert check.reason == "no container declares harbor.platform"


def test_platform_containers_fails_when_a_labelled_container_is_restarting():
    looping = Container(
        "watchtower", (), {"harbor.kind": "internal", "harbor.platform": "true"}, frozenset(),
        status="restarting", restarts=637, exit_code=1,
    )

    check = checks(containers=(ROUTED, looping))[CHECK_PLATFORM_CONTAINERS]

    assert check.state == STATE_FAILED
    assert check.reason == "container 'watchtower' is restarting (637 restarts, last exit 1)"


def test_platform_containers_fails_when_a_labelled_container_is_unhealthy():
    sick = Container(
        "portainer", (), {"harbor.platform": "true"}, frozenset(), health="unhealthy"
    )

    check = checks(containers=(sick,))[CHECK_PLATFORM_CONTAINERS]

    assert check.state == STATE_FAILED
    assert check.reason == "container 'portainer' fails its own healthcheck"


def test_platform_containers_names_the_first_broken_one_by_name():
    second = Container("watchtower", (), {"harbor.platform": "true"}, frozenset(), status="restarting")
    first = Container("portainer", (), {"harbor.platform": "true"}, frozenset(), health="unhealthy")

    check = checks(containers=(second, first))[CHECK_PLATFORM_CONTAINERS]

    assert check.reason.startswith("container 'portainer'")


def test_platform_containers_ignores_an_unlabelled_restarting_container():
    stray = Container("dev", (), {}, frozenset(), status="restarting", restarts=9)

    check = checks(containers=(ROUTED, stray))[CHECK_PLATFORM_CONTAINERS]

    assert check.state == STATE_OK


def test_platform_containers_is_unknown_without_docker():
    check = checks(docker_available=False, containers=())[CHECK_PLATFORM_CONTAINERS]

    assert check.state == STATE_UNKNOWN
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_checks.py -q`
Expected: `ImportError: cannot import name 'CHECK_PLATFORM_CONTAINERS'`.

- [ ] **Step 3: Implement**

In `src/harbor_console/checks.py`:

Change the directory import to:

```python
from harbor_console.directory import KIND_HTTP, container_finding, declared_kind, route_url
```

After `CHECK_PROBER_FRESH = "prober-fresh"` add:

```python
CHECK_PLATFORM_CONTAINERS = "platform-containers"

#: A container this repository deploys itself declares so with this label
#: (ADR 24). Traefik, Portainer and Watchtower carry it in deploy/. Nothing
#: infers it from compose project names.
LABEL_PLATFORM = "harbor.platform"
```

In `run_checks`, change the docstring's "Fixed order, six checks." to "Fixed order, seven checks." and add a seventh element to the returned tuple:

```python
        _platform_containers(docker_available, containers),
```

After `_docker` add:

```python
def _platform_containers(available: bool, containers: Sequence[Container]) -> Check:
    """The repository's own containers are platform (ADR 24).

    A declared project service being down is a row, never a check (ADR 21);
    the three containers this repository deploys are the machinery the rows
    depend on, and when one of them loops nothing else on the page says so.
    A platform container that exited with no restart policy is absent from
    `docker ps` and this check cannot see it; all three carry a policy.
    """
    if not available:
        return Check(CHECK_PLATFORM_CONTAINERS, STATE_UNKNOWN, "Docker could not be read")
    platform = sorted(
        (c for c in containers if c.labels.get(LABEL_PLATFORM) == "true"),
        key=lambda c: c.name,
    )
    for container in platform:
        finding = container_finding(container)
        if finding is not None:
            return Check(CHECK_PLATFORM_CONTAINERS, STATE_FAILED, finding.detail)
    if not platform:
        return Check(CHECK_PLATFORM_CONTAINERS, STATE_OK, f"no container declares {LABEL_PLATFORM}")
    noun = "container" if len(platform) == 1 else "containers"
    return Check(CHECK_PLATFORM_CONTAINERS, STATE_OK, f"{len(platform)} platform {noun} running")
```

Update the module docstring: the sentence "A single declared service being down is a row-level DOWN in the directory, never a check here" gains ", except for the containers this repository deploys itself, which declare `harbor.platform=true` (ADR 24)". Replace "The checks cover the three outages" with "The checks cover the four outages" and add "a hosted container crash-looping for a fortnight unseen (watchtower, 2026-10-21)" to the list.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_checks.py -q`
Expected: all pass.

Run: `uv run pytest -q`
Expected: all pass. If a test elsewhere counts checks (search `grep -rn "6 passed\|six" tests/`), update it to seven and say so in the commit.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/checks.py tests/test_checks.py
git commit -F - <<'EOF'
feat: the platform-containers check fails when a harbor.platform container is restarting or unhealthy

Seventh check. A container this repository deploys declares itself with
harbor.platform=true; nothing infers it from compose project names. Unknown
without Docker, ok when none declares it.

Assisted-by: Claude Fable 5.1 (Claude Code)
EOF
```

---

### Task 4: The page colours the two states red

**Files:**
- Modify: `src/harbor_console/web.py` (import near line 26, `_state_cell` near line 246)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `STATE_RESTARTING`, `STATE_UNHEALTHY` from Task 2.

- [ ] **Step 1: Write the failing tests**

In `tests/test_web.py`, after `test_down_is_emphasised` and its `ROUTE ERROR` sibling (near line 117), append:

```python
def test_restarting_is_emphasised():
    looping = Row("watchtower", KIND_INTERNAL, "", "watchtower", "", "RESTARTING")

    page = web.render_page(snapshot(rows=(looping,))).decode()

    assert '<span class="down">RESTARTING</span>' in page


def test_unhealthy_is_emphasised():
    sick = Row("gte-db-1", KIND_INTERNAL, "", "gte-db-1", "", "UNHEALTHY")

    page = web.render_page(snapshot(rows=(sick,))).decode()

    assert '<span class="down">UNHEALTHY</span>' in page
```

Make sure `KIND_INTERNAL` is in the file's `from harbor_console.directory import (...)` block; add it if not.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web.py -q -k emphasised`
Expected: 2 failures, the span is absent.

- [ ] **Step 3: Implement**

In `src/harbor_console/web.py`, change the directory import to include the two states:

```python
from harbor_console.directory import (
    KIND_HTTP,
    STATE_DOWN,
    STATE_RESTARTING,
    STATE_ROUTE_ERROR,
    STATE_UNHEALTHY,
    Row,
)
```

and `_state_cell` to:

```python
def _state_cell(row: Row) -> str:
    if row.state in (STATE_DOWN, STATE_ROUTE_ERROR, STATE_RESTARTING, STATE_UNHEALTHY):
        return f"<span class=\"down\">{escape(row.state)}</span>"
    return escape(row.state)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/web.py tests/test_web.py
git commit -F - <<'EOF'
feat: RESTARTING and UNHEALTHY rows render like DOWN

Assisted-by: Claude Fable 5.1 (Claude Code)
EOF
```

---

### Task 5: The repository's own containers declare `harbor.platform`

**Files:**
- Modify: `deploy/traefik/compose.yaml` (the `labels:` block near line 130)
- Modify: `deploy/hosted/compose.yaml` (both services' `labels:` blocks)
- Modify: `docs/how-docker-cloudflare-and-traefik-work.md` (the compose excerpt in "Portainer and Watchtower", near line 687)
- Test: `tests/test_deploy.py`

**Interfaces:**
- Consumes: the label name `harbor.platform` from Task 3.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_deploy.py`:

```python
def compose_services(path: Path) -> list[str]:
    """Top-level service names: two-space-indented keys under `services:`."""
    text = path.read_text(encoding="utf-8")
    body = text.split("services:", 1)[1]
    body = re.split(r"^\S", body, maxsplit=1, flags=re.MULTILINE)[0]
    return re.findall(r"^  ([A-Za-z][\w-]*):\s*$", body, re.MULTILINE)


def test_every_container_this_repo_deploys_declares_itself_platform():
    # ADR 24: the platform-containers check judges only containers that
    # carry harbor.platform=true. A service added here without it is a
    # container the check silently ignores.
    for path, expected in ((TRAEFIK_COMPOSE, ["traefik"]), (HOSTED_COMPOSE, ["portainer", "watchtower"])):
        text = path.read_text(encoding="utf-8")
        assert compose_services(path) == expected, path
        assert text.count('harbor.platform: "true"') == len(expected), path
```

`HOSTED_COMPOSE` already exists in this file from the watchtower pin guard; `TRAEFIK_COMPOSE` too.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_deploy.py -q`
Expected: 1 failure, count is 0.

- [ ] **Step 3: Add the labels**

In `deploy/traefik/compose.yaml`, in Traefik's `labels:` block after `harbor.kind: edge`:

```yaml
      # ADR 24: a container this repository deploys is platform; a crash
      # loop or failed healthcheck here trips the banner, not just a row.
      harbor.platform: "true"
```

In `deploy/hosted/compose.yaml`, after `harbor.description: "Portainer"`:

```yaml
      harbor.platform: "true"
```

and in the watchtower service's `labels:` block after `harbor.kind: internal`:

```yaml
      # ADR 24: platform. This is the container that looped 637 times unseen.
      harbor.platform: "true"
```

In `docs/how-docker-cloudflare-and-traefik-work.md`, add `      harbor.platform: "true"` to the watchtower labels in the compose excerpt (after `harbor.kind: internal`) and to Portainer's (after `traefik.docker.network: admin`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q`
Expected: all pass.

Run: `git ls-files --eol deploy/traefik/compose.yaml deploy/hosted/compose.yaml docs/how-docker-cloudflare-and-traefik-work.md`
Expected: every line shows `w/lf`. If one shows `w/crlf`, convert it back to LF before committing.

- [ ] **Step 5: Commit**

```bash
git add deploy/traefik/compose.yaml deploy/hosted/compose.yaml docs/how-docker-cloudflare-and-traefik-work.md tests/test_deploy.py
git commit -F - <<'EOF'
deploy: Traefik, Portainer and Watchtower declare harbor.platform=true

With a guard that every service in the two compose files carries it.

Assisted-by: Claude Fable 5.1 (Claude Code)
EOF
```

---

### Task 6: ADR 24 and the project docs

**Files:**
- Create: `docs/adr/0024-the-repositorys-own-containers-are-platform.md`
- Modify: `docs/adr/README.md` (the table)
- Modify: `CLAUDE.md` (the `directory.py` bullet, the `checks.py` bullet, the "Declaring a service" table)

- [ ] **Step 1: Write the ADR**

Create `docs/adr/0024-the-repositorys-own-containers-are-platform.md`:

```markdown
# 24. The repository's own containers are platform

Date: 2026-10-21

## Status

Accepted. Amends [ADR 21](0021-colour-the-platform-banner-and-nothing-else.md).

## Context

On 2026-10-21 the `watchtower` container was found restarting for the 637th
time. Docker Engine 29, installed on 2026-10-08, refuses the API version its
abandoned image pinned; it exited within a second of every start and
`restart: always` made that a loop. For a fortnight no image on the host was
updated and nothing on either surface said so.

Nothing could have. The container is declared `harbor.kind=internal`: no
route to probe, no port to check, by design. `docker.py` kept name, labels,
ports and networks from `docker inspect` and dropped `State`, so the one
piece of evidence that existed was fetched every cycle and thrown away. And
ADR 21 says a declared service being down is a `DOWN` row, never a check: a
banner red for one dev container stops meaning anything.

That rule was written for project containers. Traefik, Portainer and
Watchtower are not project containers. They come from this repository's own
compose files, `install.sh` brings them up, and the page's rows depend on
them: without Traefik there are no routes, without Watchtower no image on
the host is ever updated. A failure in one of them is the kind of failure
ADR 21 built the banner for.

## Decision

We will read each container's `State.Status`, `RestartCount`, `ExitCode` and
`State.Health.Status` from the inspect call the collector already makes, and
report a `restarting` container as a `crash-looping` finding and an
`unhealthy` one as an `unhealthy` finding, for any container Docker lists.
A declared container in either condition shows `RESTARTING` or `UNHEALTHY`
in place of whatever state its kind would have given it.

We will add a seventh platform check, `platform-containers`, that fails
when a container labelled `harbor.platform=true` is restarting or
unhealthy. The three containers this repository deploys carry the label in
their compose files, and `tests/test_deploy.py` guards that every service
there does. Nothing infers the label from compose project names or
container names; a container declares itself, as it does for everything
else here (ADR 15).

ADR 21's "a declared service being down is a `DOWN` row, never a check"
now reads "... never a check, unless the container declares itself
platform". Red still means one thing: the host's own machinery is broken.

## Consequences

- A crash loop in Traefik, Portainer or Watchtower reaches the monitor in
  the room and the page's banner within one prober cycle, and
  `harbor-console-check` exits non-zero on it, so a deploy that leaves one
  looping fails loudly.
- Every other container's loop or failed healthcheck is on the page as a
  finding and a red state cell, where it was invisible before. The banner
  stays theirs to earn: a project container adding `harbor.platform=true`
  is a one-line decision in its own compose file, and this repository
  keeps no list of who did.
- The restart count is detail text, never a trigger: it is cumulative
  since the container was created, and one restart a week ago would nag
  until the next recreate.
- A platform container that exits with no restart policy and stays down is
  absent from `docker ps`, and this check cannot see it. All three carry a
  policy; Traefik, the `unless-stopped` one, is already covered by
  `traefik-api`, `docker-provider` and `edge-listening`. Reading
  `docker ps -a` to close the gap was rejected: a deliberately stopped
  container is not a finding, and the demonstrated failure is a loop.
- No new subprocess, no new dependency, no change to the verdict file's
  shape or to the console.
```

- [ ] **Step 2: Index it and update CLAUDE.md**

In `docs/adr/README.md`, add a row after ADR 23:

```markdown
| 0024 | [The repository's own containers are platform](0024-the-repositorys-own-containers-are-platform.md) | Accepted |
```

In `CLAUDE.md`:

- In the `docker.py` bullet, after "published host ports and networks," add "plus each container's status, restart count, last exit code and healthcheck verdict,".
- In the `directory.py` bullet, change "Four findings — `undeclared-container`, `bypasses-proxy`, `route-error`, `undeclared-tailnet-listener` —" to "Six findings — `undeclared-container`, `bypasses-proxy`, `route-error`, `undeclared-tailnet-listener`, `crash-looping`, `unhealthy` —" and after the sentence ending "Absence of evidence is never a finding." add: "`crash-looping` and `unhealthy` come from Docker's own `State`, for any container it lists; a declared container in either condition shows `RESTARTING` or `UNHEALTHY` in place of its kind's state ([ADR 24](docs/adr/0024-the-repositorys-own-containers-are-platform.md))."
- In the `checks.py` bullet, change "six named platform checks out (`docker`, `traefik-api`, `docker-provider`, `own-route`, `certificate`, `edge-listening`)" to "seven named platform checks out (`docker`, `traefik-api`, `docker-provider`, `own-route`, `certificate`, `edge-listening`, `platform-containers`)", change "Platform only; a declared service being down is a `DOWN` row, never a check." to "Platform only; a declared service being down is a `DOWN` row, never a check, unless the container declares `harbor.platform=true`, which the three containers this repo deploys do ([ADR 24](docs/adr/0024-the-repositorys-own-containers-are-platform.md))." and "`freshness_check` is the seventh" to "`freshness_check` is the eighth".
- In "Declaring a service", after the line "Any container may add `harbor.description=<one line>` for the directory." add: "A container this repository deploys itself also carries `harbor.platform=true`, which puts its crash loops and failed healthchecks on the red banner (ADR 24); project containers do not."

- [ ] **Step 3: Run the suite and check line endings**

Run: `uv run pytest -q`
Expected: all pass.

Run: `git ls-files --eol CLAUDE.md docs/adr/README.md`
Expected: `w/lf`.

- [ ] **Step 4: Commit**

```bash
git add docs/adr/0024-the-repositorys-own-containers-are-platform.md docs/adr/README.md CLAUDE.md
git commit -F - <<'EOF'
docs: ADR 24, the repository's own containers are platform

Assisted-by: Claude Fable 5.1 (Claude Code)
EOF
```
