# Review Fixes Implementation Plan (2026-09-09 code review)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 10 confirmed correctness bugs from the 2026-09-09 code review of `src/` (ports allocation and web reporting).

**Architecture:** Each fix is small and local, follows the repo's collect/render/coordinate split, and lands with a failing test first. No new modules except code inside existing ones; one new function in `tailnet.py`.

**Tech Stack:** Python 3.13, uv, pytest, stdlib only (no new runtime deps — hard constraint).

## Global Constraints

- Stdlib `http.server` and `tomllib` only; **no new runtime dependencies** (CLAUDE.md hard constraint).
- `ports/allocate.py` and `reconcile.py` stay **pure**: no I/O, no subprocess, no sockets.
- Collectors **degrade, never raise** (except `tailnet.tailscale_address`, the ledger duplicate-key check, and declaration load failures in scan/sync).
- Every whole-file write goes through `ports/atomic.py`; `.env` is written last.
- TDD: write the failing test, watch it fail, implement, watch it pass, commit.
- Run tests with `uv run pytest` (never bare pytest/pip). Do NOT chain shell commands with `&&` — run them as separate commands.
- Commit messages: conventional style (`fix(scope): ...`), and end each commit message body with the two trailer lines the session requires (Co-Authored-By: Claude Fable 5 <noreply@anthropic.com> and the Claude-Session URL) if provided in your dispatch.
- `pyproject.toml` sets `pythonpath = ["src"]`; tests import `harbor_console` directly.

---

### Task 1: `docker ps` count gets a timeout

**Files:**
- Modify: `src/harbor_console/system.py:32-48` (`get_docker_container_count`)
- Test: `tests/test_system.py`

**Interfaces:**
- Produces: `get_docker_container_count(run=subprocess.run, timeout=DOCKER_TIMEOUT_SECONDS) -> int` — same return contract (0 on any failure), now injectable and bounded.

Bug: `get_docker_container_count()` runs `docker ps -q` with no `timeout=`. It is called every prober cycle via `webapp.collect_snapshot`, so a wedged Docker daemon blocks the prober thread forever — the exact hang `docker.DOCKER_TIMEOUT_SECONDS` exists to prevent in `docker.py`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_system.py`; add `import subprocess` and `from types import SimpleNamespace` if missing):

```python
def test_docker_count_gives_the_subprocess_a_timeout():
    seen = {}

    def run(*_args, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(stdout="", returncode=0)

    system.get_docker_container_count(run=run)

    assert seen["timeout"] == 2.0


def test_docker_count_treats_a_hung_daemon_as_zero():
    def run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["docker", "ps", "-q"], timeout=2.0)

    assert system.get_docker_container_count(run=run) == 0
```

Match the existing import style of `tests/test_system.py` (it may import functions directly rather than the module; adapt the calls accordingly).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_system.py -v`
Expected: FAIL — `get_docker_container_count() got an unexpected keyword argument 'run'`.

- [ ] **Step 3: Implement** — replace `get_docker_container_count` in `src/harbor_console/system.py`:

```python
from harbor_console.docker import DOCKER_TIMEOUT_SECONDS


def get_docker_container_count(
    run: Callable[..., object] = subprocess.run,
    timeout: float = DOCKER_TIMEOUT_SECONDS,
) -> int:
    """Return the number of running Docker containers.

    Bounded for the same reason `docker.running_containers` is: this runs in
    the web prober thread every cycle, and a wedged daemon with no bound
    blocks that thread forever while the last snapshot is served as current.
    """
    try:
        result = run(
            ["docker", "ps", "-q"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 0
    except (FileNotFoundError, OSError):
        return 0

    if result.returncode != 0:  # type: ignore[attr-defined]
        return 0

    lines = [line for line in result.stdout.splitlines() if line.strip()]  # type: ignore[attr-defined]
    return len(lines)
```

Add `from collections.abc import Callable` to the imports. Import `DOCKER_TIMEOUT_SECONDS` from `harbor_console.docker` (both modules are collectors; no cycle — `docker.py` imports nothing from `system.py`).

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/system.py tests/test_system.py
git commit -m "fix(system): bound docker ps with the collector timeout"
```

---

### Task 2: Docker port-range publishes are parsed

**Files:**
- Modify: `src/harbor_console/docker.py:107` (`_PUBLISHED`) and `_publish_pairs`
- Test: `tests/test_docker.py`

Bug: `_PUBLISHED` requires `:<digits>->`, so Docker's range syntax (`0.0.0.0:8000-8005->8000-8005/tcp`) matches nothing and every published port of that container vanishes — the allocator then denies grandfathering and reconcile loses all findings for it.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_docker.py`):

```python
def test_a_port_range_publish_expands_to_every_port_in_the_range():
    out = "app\t0.0.0.0:8000-8002->8000-8002/tcp\n"

    result = running_containers(run=fake_run(out))

    assert result[0].published == (
        ("0.0.0.0", 8000),
        ("0.0.0.0", 8001),
        ("0.0.0.0", 8002),
    )


def test_a_backwards_range_yields_only_its_first_port():
    result = running_containers(run=fake_run("app\t0.0.0.0:9000-8000->9000/tcp\n"))

    assert result[0].published == (("0.0.0.0", 9000),)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_docker.py -v`
Expected: the first new test FAILS with `published == ()`.

- [ ] **Step 3: Implement** in `src/harbor_console/docker.py`:

```python
#: Matches the published half of `0.0.0.0:8080->8080/tcp`, `:::8080->8080/tcp`
#: and the range form `0.0.0.0:8000-8005->8000-8005/tcp`.
_PUBLISHED = re.compile(r"^(?P<addr>.*):(?P<lo>\d+)(?:-(?P<hi>\d+))?->")
```

and in `_publish_pairs`, replace the single `pairs.append(...)` with:

```python
        addr = IPV4_ANY if addr in ("", IPV6_ANY) else addr
        lo = int(match.group("lo"))
        hi = int(match.group("hi") or lo)
        for port in range(lo, hi + 1) if hi >= lo else (lo,):
            pairs.append((addr, port))
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/docker.py tests/test_docker.py
git commit -m "fix(docker): parse port-range publishes instead of dropping them"
```

---

### Task 3: `write_assigned` agrees with `tomllib` about what a declaration contains

**Files:**
- Modify: `src/harbor_console/ports/declaration.py:611-643` (`_port_block_bounds`, `_is_table_header`, `_block_name`)
- Test: `tests/test_ports_declaration.py`

Bug (two halves, one cause — the hand-rolled writer parses less TOML than `tomllib` reads):
1. `_block_name` strips only double quotes, so `name = 'web'` (valid TOML) never matches and `write_assigned` raises `DeclarationError` mid-sync.
2. `_port_block_bounds` recognises a header only when `line.strip() == "[[port]]"` exactly, so `[[port]]  # comment` is invisible.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_ports_declaration.py`; use the file's existing helpers/imports — it already imports `write_assigned` or import it):

```python
def test_write_assigned_accepts_a_single_quoted_name(tmp_path):
    path = tmp_path / ".harbor.toml"
    path.write_text(
        "project = \"p\"\nhost = \"h\"\n\n[[port]]\nname = 'web'\nwant = 8080\n",
        encoding="utf-8",
    )

    write_assigned(path, "web", 8100)

    assert load_declaration(path).ports[0].assigned == 8100


def test_write_assigned_sees_a_port_header_with_a_trailing_comment(tmp_path):
    path = tmp_path / ".harbor.toml"
    path.write_text(
        'project = "p"\nhost = "h"\n\n[[port]]  # the web port\nname = "web"\n',
        encoding="utf-8",
    )

    write_assigned(path, "web", 8100)

    assert load_declaration(path).ports[0].assigned == 8100
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_ports_declaration.py -v`
Expected: both FAIL with `DeclarationError: ... no [[port]] named 'web'`.

- [ ] **Step 3: Implement** in `src/harbor_console/ports/declaration.py`:

```python
def _table_text(line: str) -> str:
    """A line with any trailing TOML comment removed, stripped.

    `tomllib` accepts `[[port]]  # comment`; the writer must see the same
    file the reader saw, or a grant is made and then cannot be written back.
    A `#` cannot appear inside a table header or an identifier this tool
    accepts, so splitting on it is safe here.
    """
    return line.split("#", 1)[0].strip()
```

Change `_port_block_bounds` to use it:

```python
    starts = [i for i, line in enumerate(lines) if _table_text(line) == "[[port]]"]
```

Change `_is_table_header`:

```python
def _is_table_header(line: str) -> bool:
    """True if `line` opens a top-level TOML table, e.g. `[foo]` or `[[foo]]`."""
    stripped = _table_text(line)
    return len(stripped) > 2 and stripped.startswith("[") and stripped.endswith("]")
```

Change `_block_name` to accept both TOML quote styles:

```python
def _block_name(block: list[str]) -> str | None:
    for line in block:
        key, _, value = line.partition("=")
        if key.strip() == "name":
            text = value.split("#")[0].strip()
            if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
                text = text[1:-1]
            return text
    return None
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/ports/declaration.py tests/test_ports_declaration.py
git commit -m "fix(ports): teach write_assigned the TOML tomllib already accepts"
```

---

### Task 4: `/ports.json` refuses while collection is failing

**Files:**
- Modify: `src/harbor_console/web.py:27-96` (`_ports_refusals` and the reason constants)
- Test: `tests/test_web.py`

Bug: `_ports_refusals` gates only on `snapshot.probed` and `docker_available`. When the prober fails a cycle, `probe_loop` republishes the *last good* snapshot with `collection_error` set — and `/ports.json` keeps serving that day-old listener list as 200. The allocator then treats a since-bound port as free.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_web.py`; the file has a `snapshot(**overrides)` helper and existing handler tests to model the request plumbing on):

```python
def test_ports_json_refuses_while_collection_is_failing():
    snap = snapshot(collection_error="psutil raised")

    reasons = web._ports_refusals(snap)

    assert any("stale" in reason for reason in reasons)


def test_ports_json_answers_again_once_a_cycle_succeeds():
    assert web._ports_refusals(snapshot(collection_error=None)) == ()
```

(Import `harbor_console.web as web` if the test file imports names individually; follow its style. If an existing handler-level 503 test exists for the unprobed window, add a handler-level assertion mirroring it with `collection_error` set — same pattern, expect 503.)

- [ ] **Step 2: Run to verify the first fails**

Run: `uv run pytest tests/test_web.py -v`
Expected: `test_ports_json_refuses_while_collection_is_failing` FAILS (empty tuple).

- [ ] **Step 3: Implement** in `src/harbor_console/web.py` — add a third reason constant beside the two existing ones:

```python
STALE_REASON = (
    "the last collection cycle failed, so this listener data is stale: "
)
```

and extend `_ports_refusals`:

```python
def _ports_refusals(snapshot: Snapshot) -> tuple[str, ...]:
    """Every reason `/ports.json` must refuse for this snapshot.

    Empty means the payload is answerable. Three conditions: a snapshot
    nothing has been collected into yet, one collected while Docker could
    not be read, and one whose *latest* cycle failed. The third closes the
    window the first two miss: after one good cycle, a permanently failing
    prober would otherwise keep serving that cycle's listeners as a 200
    forever, and the allocator would grant against sockets bound since.
    The HTML page keeps serving in all three windows; it reports the
    failure in its own banner.
    """
    reasons = []
    if not snapshot.probed:
        reasons.append(UNPROBED_REASON)
    if not snapshot.docker_available:
        reasons.append(DOCKER_REASON)
    if snapshot.collection_error is not None:
        reasons.append(STALE_REASON + snapshot.collection_error)
    return tuple(reasons)
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/web.py tests/test_web.py
git commit -m "fix(web): 503 /ports.json while the collection cycle is failing"
```

---

### Task 5: the allocator asks for `/ports.json` where the page actually listens

**Files:**
- Modify: `src/harbor_console/tailnet.py` (add `peer_address`)
- Modify: `src/harbor_console/ports/cli.py:784-812` (`ports_url`)
- Test: `tests/test_tailnet.py`, `tests/test_ports_cli.py`

Bug: `ports_url` builds `http://{lease.host}:{port}/ports.json` from the ledger's hand-authored hostname. The web service binds the Tailscale address only (ADR 7); on a box where `hpz440` resolves via LAN DNS, `fetch_live` connects where nothing listens and every scan/sync loses its liveness guard — the same hostname-vs-tailnet mismatch commit c208fd6 fixed in the prober.

The ledger's `harbor-console/web` lease records `addr = "0.0.0.0"`, so the addr alone does not answer; the client resolves the host's tailnet address via `tailscale ip -4 <host>` (the dev box is on the tailnet — that is the only way it reaches the page at all), falling back to the hostname when tailscale cannot say.

- [ ] **Step 1: Write the failing tests.**

Append to `tests/test_tailnet.py` (mirror its existing fake-run style — read the file first; it tests `tailscale_address` with an injected `run`):

```python
def test_peer_address_returns_the_peers_tailnet_ipv4():
    def run(*_args, **_kwargs):
        return SimpleNamespace(stdout="100.69.239.123\n", returncode=0)

    assert tailnet.peer_address("hpz440", run=run) == "100.69.239.123"


def test_peer_address_degrades_to_none_when_tailscale_cannot_say():
    assert tailnet.peer_address("hpz440", run=_raiser(FileNotFoundError())) is None

    def failed(*_args, **_kwargs):
        return SimpleNamespace(stdout="", returncode=1)

    assert tailnet.peer_address("hpz440", run=failed) is None
```

(Define `_raiser` locally or reuse the file's existing helper; adapt names to the file's conventions.)

Append to `tests/test_ports_cli.py`:

```python
def test_ports_url_resolves_a_wildcard_lease_through_tailscale():
    leases = [Lease("harbor-console", "web", "hpz440", "0.0.0.0", 80, TODAY)]

    url = cli.ports_url(leases, resolve=lambda host: "100.69.239.123")

    assert url == "http://100.69.239.123:80/ports.json"


def test_ports_url_falls_back_to_the_hostname_when_tailscale_cannot_say():
    leases = [Lease("harbor-console", "web", "hpz440", "0.0.0.0", 80, TODAY)]

    url = cli.ports_url(leases, resolve=lambda host: None)

    assert url == "http://hpz440:80/ports.json"


def test_ports_url_uses_a_specific_leased_addr_without_asking_tailscale():
    leases = [Lease("harbor-console", "web", "hpz440", "100.69.239.123", 8090, TODAY)]

    def explode(_host):
        raise AssertionError("a specific addr needs no resolution")

    assert cli.ports_url(leases, resolve=explode) == "http://100.69.239.123:8090/ports.json"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_tailnet.py tests/test_ports_cli.py -v`
Expected: FAIL — `tailnet` has no `peer_address`; `ports_url` takes no `resolve`.

- [ ] **Step 3: Implement.**

In `src/harbor_console/tailnet.py`, add (keeping the module's existing subprocess style):

```python
PEER_TIMEOUT_SECONDS = 2.0


def peer_address(
    host: str,
    run: Callable[..., object] = subprocess.run,
    timeout: float = PEER_TIMEOUT_SECONDS,
) -> str | None:
    """The tailnet IPv4 of a peer, or None when tailscale cannot say.

    Degrades rather than raising, unlike `tailscale_address`: the caller is
    the allocator CLI, which already treats unreachable live state as a
    refusal to grant, so an unanswerable lookup is a fallback, not a fault.
    """
    try:
        result = run(
            ["tailscale", "ip", "-4", host],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:  # type: ignore[attr-defined]
        return None
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]  # type: ignore[attr-defined]
    return lines[0] if lines else None
```

In `src/harbor_console/ports/cli.py`, change `ports_url` (add `from collections.abc import Callable` if needed, `from harbor_console import tailnet`, and `from harbor_console.ports.keys import ANY_ADDR` alongside the existing `env_var_name` import):

```python
def ports_url(
    leases: Sequence[Lease],
    resolve: Callable[[str], str | None] = tailnet.peer_address,
) -> str | None:
```

and replace the final two lines of its body:

```python
    lease = mine[0]
    # The page binds the Tailscale address only (ADR 7), so the ledger's
    # hand-authored hostname -- which LAN DNS may resolve elsewhere -- is the
    # last resort, not the first. A specific leased addr is already the
    # answer; a wildcard lease is resolved through tailscale from this end of
    # the wire, the same rule c208fd6 gave the prober on the other end.
    if lease.addr != ANY_ADDR:
        host = lease.addr
    else:
        host = resolve(lease.host) or lease.host
    return f"http://{host}:{lease.port}/ports.json"
```

Keep the existing docstring, appending a line about the resolution rule if it reads better to you.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/tailnet.py src/harbor_console/ports/cli.py tests/test_tailnet.py tests/test_ports_cli.py
git commit -m "fix(ports): resolve /ports.json through the tailnet, not LAN DNS"
```

---

### Task 6: probe results are keyed by the whole lease identity

**Files:**
- Modify: `src/harbor_console/webapp.py:658-663` (the `health` dict in `collect_snapshot`)
- Modify: `src/harbor_console/web.py:256` (`_services_table` lookup)
- Modify: `src/harbor_console/snapshot.py:726` (`health` annotation)
- Test: `tests/test_webapp.py`, `tests/test_web.py`

Bug: `collect_snapshot` keys `health` on `(project, name)` while lease identity everywhere else is `(project, name, host)`. Two hosts' leases for the same project/name collide; one probe result silently overwrites the other, and a dead local service can render UP because the other host answered.

- [ ] **Step 1: Write the failing test** (append to `tests/test_webapp.py`, reusing its existing fakes for collector/listeners/containers — read the file's `collect_snapshot` tests first and copy their fixture style):

```python
def test_probe_results_do_not_collide_across_hosts():
    leases = (
        Lease("gte", "web", "hpz440", "0.0.0.0", 8080, date(2026, 9, 1)),
        Lease("gte", "web", "elsewhere", "0.0.0.0", 8080, date(2026, 9, 1)),
    )

    def prober(target, _port):
        # The local lease probes at an address; the remote one at its hostname.
        up = target == "elsewhere"
        return Health(up=up, state=None, summary=None, detail=(), warning=None)

    snap = collect_snapshot(
        leases,
        "hpz440",
        datetime(2026, 9, 9, 12, 0, 0),
        collector=lambda: dict(METRICS),
        listeners=lambda: (),
        containers=lambda: (),
        prober=prober,
        ledger_mtime=lambda: None,
        proxies=lambda: (),
    )

    assert snap.health[("gte", "web", "hpz440")].up is False
    assert snap.health[("gte", "web", "elsewhere")].up is True
```

(If `tests/test_webapp.py` has no `METRICS` dict, reuse whatever metrics fixture its other `collect_snapshot` tests use.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_webapp.py -v`
Expected: the new test FAILS with `KeyError` on the 3-tuple. Existing tests keyed on 2-tuples still pass at this point.

- [ ] **Step 3: Implement.**

`src/harbor_console/webapp.py`, in `collect_snapshot`:

```python
    health = {
        (lease.project, lease.name, lease.host): prober(
            probe_target(lease, host, tailnet_address), lease.port
        )
        for lease in held
    }
```

`src/harbor_console/web.py`, in `_services_table`:

```python
        health = snapshot.health.get((lease.project, lease.name, lease.host))
```

`src/harbor_console/snapshot.py`, the field annotation:

```python
    health: dict[tuple[str, str, str], Health] = field(default_factory=dict)
```

and note the key in its docstring line if one exists.

- [ ] **Step 4: Fix the tests the key change breaks.**

Run: `uv run pytest tests/test_webapp.py tests/test_web.py -v` and update every `health={("proj", "name"): ...}` fixture and `snapshot.health[("proj", "name")]` assertion to include the host (e.g. `("gte", "console", "hpz440")` — the host used by that file's lease fixtures). The `snapshot()` helper in `tests/test_web.py` has one such key.

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_console/webapp.py src/harbor_console/web.py src/harbor_console/snapshot.py tests/test_webapp.py tests/test_web.py
git commit -m "fix(web): key probe health by the whole lease identity"
```

---

### Task 7: a serve front whose backends are leased is not permanent drift

**Files:**
- Modify: `src/harbor_console/reconcile.py:377-436` (`_undeclared_tailnet_listeners`)
- Test: `tests/test_reconcile.py`

Bug: tailscaled's own front ports (443/8443 on the tailnet address) can never be covered by a lease — the ledger has no way to declare a front — so `undeclared-tailnet-listener` stands permanently on a fully-adopted healthy host, training operators to ignore the drift section.

Fix rule: when serve knowledge is present and **every** proxy on the port forwards to a backend some local lease covers, the listener is fully accounted for — the lease's directory row already shows the front's URL (`fronts_for`) — so no finding. A front proxying to an unleased backend still reports, with the existing clause. Absent serve knowledge (empty `proxies`), behavior is unchanged: the finding stands, reported as absence of knowledge.

- [ ] **Step 1: Write the failing test** (append to `tests/test_reconcile.py`, following its fixture style — read its existing `_undeclared_tailnet_listeners`/serve-clause tests first):

```python
def test_a_serve_front_whose_backend_is_leased_is_not_drift():
    tailnet = "100.69.239.123"
    leases = [Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 9, 1))]
    listeners = [
        Listener("0.0.0.0", 8080, None),
        Listener(tailnet, 8443, None),  # tailscaled's front
    ]
    containers = (Container("gte", (("0.0.0.0", 8080),)),)
    proxies = (Proxy(8443, "/", "127.0.0.1", 8080),)

    findings = find_drift(
        leases, listeners, containers, "hpz440",
        tailnet_address=tailnet, proxies=proxies,
    )

    assert findings == ()


def test_a_serve_front_to_an_unleased_backend_is_still_reported():
    tailnet = "100.69.239.123"
    listeners = [Listener(tailnet, 8443, None)]
    proxies = (Proxy(8443, "/", "127.0.0.1", 9999),)

    findings = find_drift(
        [], listeners, (), "hpz440", tailnet_address=tailnet, proxies=proxies,
    )

    assert len(findings) == 1
    assert findings[0].kind == "undeclared-tailnet-listener"
    assert "nothing declares" in findings[0].detail
```

(`find_drift` requires Docker evidence for this class; `()` is an ordinary empty tuple, not `DOCKER_UNAVAILABLE`, so the second test's findings are produced.)

- [ ] **Step 2: Run to verify the first fails**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: the first new test FAILS — one `undeclared-tailnet-listener` finding for 8443 with the proxy clause.

- [ ] **Step 3: Implement** in `_undeclared_tailnet_listeners` — after the `_covers(published, addr, port)` check and before the clause is built, insert:

```python
        # A front every one of whose backends is leased is fully accounted
        # for: somebody configured serve to publish a declared service, and
        # the directory row for that lease already leads with the front's
        # URL. A standing finding here would never clear on a healthy,
        # fully-adopted host, which teaches operators to skip the one
        # section that exists to be read. A front to an *unleased* backend
        # still reports, and absent serve knowledge nothing is suppressed --
        # absence of knowledge must not read as "accounted for".
        behind = [proxy for proxy in proxies if proxy.port == port]
        if behind and all(
            any(
                lease.port == proxy.backend_port
                and addrs_overlap(lease.addr, proxy.backend_addr)
                for lease in mine
            )
            for proxy in behind
        ):
            continue
```

Also update the function's docstring to mention the suppression rule (one sentence).

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest`
Expected: all pass. If an existing test asserted the clause on a *leased* backend (the "which gte leases as console" wording), it now expects no finding — update that test to use an unleased backend for the clause assertion, preserving clause coverage.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/reconcile.py tests/test_reconcile.py
git commit -m "fix(reconcile): a serve front onto leased backends is accounted for"
```

---

### Task 8: an addr-only change reaches the ledger

**Files:**
- Modify: `src/harbor_console/ports/allocate.py:159-183` (rule 1 uncontended branch)
- Modify: `src/harbor_console/ports/cli.py:190` (the `changes` filter)
- Test: `tests/test_ports_allocate.py`, `tests/test_ports_cli.py`

Bug: `cli.run` filters every `action == "keep"` decision before `apply_decisions`, so a keep whose addr changed (declaration widened `127.0.0.1` → `0.0.0.0`, uncontended) never updates the ledger — violating `apply_decisions`' documented contract (allocate.py:342-343). The ledger goes on saying `127.0.0.1:8080`, a later `100.x:8080` declaration passes `_is_free`, and the founding collision is reintroduced by the allocator itself.

- [ ] **Step 1: Write the failing tests.**

Append to `tests/test_ports_allocate.py`:

```python
def test_an_uncontended_addr_change_is_reported_not_swallowed():
    leases = [Lease("p", "web", "hpz440", "127.0.0.1", 8080, date(2026, 8, 1))]

    [decision] = decide([decl("p", "web", assigned=8080, addr="0.0.0.0")], leases, live(), TODAY)

    assert decision.addr == "0.0.0.0"
    assert decision.port == 8080
    assert "addr updated" in decision.reason
```

Append to `tests/test_ports_cli.py`:

```python
def test_sync_records_a_widened_addr_in_the_ledger(tmp_path: Path):
    project = tmp_path / "alpha"
    project.mkdir()
    (project / ".harbor.toml").write_text(
        'project = "alpha"\nhost = "hpz440"\n\n[[port]]\nname = "web"\n'
        "want = 8080\nassigned = 8080\n",
        encoding="utf-8",
    )
    ledger_path = tmp_path / "services.toml"
    save_leases(ledger_path, [Lease("alpha", "web", "hpz440", "127.0.0.1", 8080, TODAY)])

    code, output = run(["sync"], tmp_path, ledger_path)

    [lease] = load_leases(ledger_path)
    assert lease.addr == "0.0.0.0"
    assert "alpha/web" in output
```

(The declaration has no `addr` key, so it defaults to `0.0.0.0`, widening the loopback lease.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_ports_allocate.py tests/test_ports_cli.py -v`
Expected: the allocate test FAILS on the reason (current reason is "already leased"); the cli test FAILS with `lease.addr == "127.0.0.1"`.

- [ ] **Step 3: Implement.**

`src/harbor_console/ports/allocate.py`, in `_decide_one`'s `blocker is None` branch, replace:

```python
        if blocker is None:
            if own.port == request.assigned:
                return make("keep", own.port, "already leased")
            return make("grant", own.port, "ledger holds")
```

with:

```python
        if blocker is None:
            action = "keep" if own.port == request.assigned else "grant"
            if own.addr != addr:
                # The key is changing even though the port is not. The reason
                # says so, and the CLI forwards any keep whose key differs
                # from its lease to `apply_decisions`, whose contract has
                # always been that an addr-only change reaches the ledger.
                return make(action, own.port, f"addr updated from {own.addr} to {addr}")
            if action == "keep":
                return make("keep", own.port, "already leased")
            return make("grant", own.port, "ledger holds")
```

`src/harbor_console/ports/cli.py`, replace line 190 (`changes = [decision for decision in decisions if decision.action != "keep"]`) with a call to a new module-level helper:

```python
    changes = _ledger_changes(decisions, leases)
```

```python
def _ledger_changes(
    decisions: Sequence[Decision], leases: Sequence[Lease]
) -> list[Decision]:
    """Every decision that would change the ledger, addr-only keeps included.

    A keep is not always a no-op: a declaration that widened its addr while
    staying uncontended keeps its port, and `apply_decisions`' contract is
    that the new key still reaches the ledger. Filtering on the action alone
    dropped exactly those -- `sync` printed "up to date" over a ledger still
    claiming the narrow addr, which a later declaration on a non-overlapping
    addr could then be granted against, reintroducing the founding collision.
    """
    by_identity = {
        (lease.project, lease.name, lease.host): lease for lease in leases
    }
    changes = []
    for decision in decisions:
        if decision.action != "keep":
            changes.append(decision)
            continue
        lease = by_identity.get((decision.project, decision.port_name, decision.host))
        if lease is None or (lease.addr, lease.port) != (decision.addr, decision.port):
            changes.append(decision)
    return changes
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest`
Expected: all pass (the blocked-widening path already reports `bind=own.addr`, so its keeps have an unchanged key and stay filtered).

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/ports/allocate.py src/harbor_console/ports/cli.py tests/test_ports_allocate.py tests/test_ports_cli.py
git commit -m "fix(ports): an uncontended addr change reaches the ledger"
```

---

### Task 9: editing `want` does what HARBOR_PORTS.md says it does

**Files:**
- Modify: `src/harbor_console/ports/allocate.py` (`Decision`, `_decide_one` rule 1)
- Modify: `src/harbor_console/ports/cli.py` (surface decision notes as warnings)
- Create: `docs/adr/0014-want-edits-move-an-uncontended-lease.md`
- Test: `tests/test_ports_allocate.py`, `tests/test_ports_cli.py`

Bug: every participating project's `HARBOR_PORTS.md` says "To change a port, edit `want` and run sync. You may not get what you asked for — if another project already holds it, you are moved and told so." But rule 1 returns keep on an uncontended existing lease **before `want` is ever read**: the operator edits `want`, runs sync, sees "up to date", and nothing anywhere says the edit was ignored.

Fix policy (this aligns code with the shipped, documented contract):
- An existing uncontended lease whose declaration `want`s a *different* port **moves to it when that key is free** (no lease, no promise, nothing listening). Action is `"reassign"`, so `sync --new-only` (the scheduled timer) withholds it — a timer never renumbers; only a manual sync applies the move.
- When the wanted key is not free, the lease is **kept** and the refusal is *said*: `Decision` gains an optional `note`, and the CLI prints notes as warnings (non-zero exit, like compose-default warnings).
- `want == own.port` or `want` unset: unchanged behavior.

- [ ] **Step 1: Write the failing tests.**

In `tests/test_ports_allocate.py`, **replace** `test_existing_assignment_held_by_this_project_is_kept` (it encodes the bug: want=8080 free, lease 8090, asserts keep) with:

```python
def test_an_unchanged_want_keeps_the_lease():
    leases = [Lease("p", "web", "hpz440", "0.0.0.0", 8090, date(2026, 8, 1))]

    [decision] = decide([decl("p", "web", want=8090, assigned=8090)], leases, live(), TODAY)

    assert decision.action == "keep"
    assert decision.port == 8090


def test_editing_want_moves_an_uncontended_lease_to_a_free_port():
    leases = [Lease("p", "web", "hpz440", "0.0.0.0", 8090, date(2026, 8, 1))]

    [decision] = decide([decl("p", "web", want=8080, assigned=8090)], leases, live(), TODAY)

    assert decision.action == "reassign"
    assert decision.port == 8080
    assert "preferred" in decision.reason


def test_a_wanted_port_someone_holds_keeps_the_lease_and_says_so():
    leases = [
        Lease("p", "web", "hpz440", "0.0.0.0", 8090, date(2026, 8, 1)),
        Lease("q", "web", "hpz440", "0.0.0.0", 8080, date(2026, 7, 1)),
    ]
    declarations = [
        decl("p", "web", want=8080, assigned=8090),
        decl("q", "web", want=8080, assigned=8080),
    ]

    first, second = decide(declarations, leases, live(), TODAY)

    assert first.action == "keep"
    assert first.port == 8090
    assert first.note is not None
    assert "8080" in first.note
    assert "q/web" in first.note
    assert second.action == "keep"
    assert second.note is None


def test_a_wanted_port_with_a_listener_keeps_the_lease_and_says_so():
    leases = [Lease("p", "web", "hpz440", "0.0.0.0", 8090, date(2026, 8, 1))]
    state = live(("0.0.0.0", 8080, "somebody"))

    [decision] = decide([decl("p", "web", want=8080, assigned=8090)], leases, state, TODAY)

    assert decision.action == "keep"
    assert decision.port == 8090
    assert decision.note is not None
```

In `tests/test_ports_cli.py`:

```python
def test_sync_moves_a_project_whose_want_changed(tmp_path: Path):
    project = make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"
    run(["sync"], tmp_path, ledger_path)

    # The operator edits want, as HARBOR_PORTS.md tells them to.
    body = (project / ".harbor.toml").read_text(encoding="utf-8")
    (project / ".harbor.toml").write_text(
        body.replace("want = 8080", "want = 8200"), encoding="utf-8"
    )

    code, output = run(["sync"], tmp_path, ledger_path)

    assert [lease.port for lease in load_leases(ledger_path)] == [8200]
    assert "HARBOR_PORT_WEB=8200" in (project / ".env").read_text(encoding="utf-8")
    assert "8200" in output


def test_new_only_withholds_a_want_move(tmp_path: Path):
    project = make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"
    run(["sync"], tmp_path, ledger_path)
    body = (project / ".harbor.toml").read_text(encoding="utf-8")
    (project / ".harbor.toml").write_text(
        body.replace("want = 8080", "want = 8200"), encoding="utf-8"
    )

    code, output = run(["sync", "--new-only"], tmp_path, ledger_path)

    assert [lease.port for lease in load_leases(ledger_path)] == [8080]
    assert "withheld" in output


def test_sync_warns_when_an_edited_want_is_unavailable(tmp_path: Path):
    make_project(tmp_path, "alpha", 8080)
    make_project(tmp_path, "beta", 8081)
    ledger_path = tmp_path / "services.toml"
    run(["sync"], tmp_path, ledger_path)

    beta = tmp_path / "beta" / ".harbor.toml"
    body = beta.read_text(encoding="utf-8")
    beta.write_text(body.replace("want = 8081", "want = 8080"), encoding="utf-8")

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 1
    assert "warning" in output
    assert "8080" in output
    assert [lease.port for lease in sorted(load_leases(ledger_path), key=lambda l: l.project)] == [8080, 8081]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_ports_allocate.py tests/test_ports_cli.py -v`
Expected: the want-move tests FAIL (keep instead of reassign); `Decision` has no `note`.

- [ ] **Step 3: Implement the policy** in `src/harbor_console/ports/allocate.py`.

Add the field to `Decision`:

```python
    incumbent: Lease | None = None
    #: A refusal worth saying out loud even though the decision is a no-op:
    #: an edited `want` that could not be honoured. The CLI prints it as a
    #: warning, because HARBOR_PORTS.md promises the operator is "moved and
    #: told so" -- and a silent keep tells nobody anything.
    note: str | None = None
```

Give `make` a `note` pass-through (`note: str | None = None` parameter, forwarded to `Decision`).

In `_decide_one`, extend the `blocker is None` branch from Task 8 so it becomes:

```python
        if blocker is None:
            action = "keep" if own.port == request.assigned else "grant"
            if request.want is not None and request.want != own.port:
                # HARBOR_PORTS.md's contract: edit `want`, run sync, and you
                # are either moved or told why not. The move is a "reassign"
                # so `sync --new-only` -- the timer -- withholds it; only a
                # manual sync renumbers a project.
                w_holder = _holder(leases, host, addr, request.want, exclude=identity)
                w_promise = _promised_by(taken, host, addr, request.want, exclude=identity)
                if (
                    w_holder is None
                    and w_promise is None
                    and _is_free(request.want, host, addr, leases, taken, live)
                ):
                    return make("reassign", request.want, f"moved to preferred {request.want}")
                if w_holder is not None:
                    blocked_by = f"{w_holder.project}/{w_holder.name} holds it"
                elif w_promise is not None:
                    blocked_by = f"{w_promise.project}/{w_promise.port_name} was promised it this run"
                else:
                    blocked_by = "something is listening on it"
                note = (
                    f"{declaration.project}/{request.name}: want {request.want} "
                    f"is unavailable ({blocked_by}); keeping {own.port}"
                )
                if own.addr != addr:
                    return make(action, own.port, f"addr updated from {own.addr} to {addr}", note=note)
                return make(action, own.port, "already leased" if action == "keep" else "ledger holds", note=note)
            if own.addr != addr:
                return make(action, own.port, f"addr updated from {own.addr} to {addr}")
            if action == "keep":
                return make("keep", own.port, "already leased")
            return make("grant", own.port, "ledger holds")
```

- [ ] **Step 4: Surface notes in the CLI** (`src/harbor_console/ports/cli.py`). In `run()`, right after `warnings = _compose_warnings(declarations, env_values)` add:

```python
    notes = [decision.note for decision in decisions if decision.note is not None]
    warnings = notes + warnings
```

And in the no-live-state refusal branch, change the `_report` call's warnings argument from `_compose_warnings(declarations, held_values)` to `notes + _compose_warnings(declarations, held_values)`.

(Notes flow through the existing `warning:` printing and the existing exit-code logic — `scan` exits 1 on warnings, `sync` returns `EXIT_PENDING` when warnings remain. That is the same permanence contract compose-default warnings already have.)

- [ ] **Step 5: Run the whole suite and repair casualties**

Run: `uv run pytest`
Expected: mostly pass. Known interactions to check, not silence:
- `test_incumbent_is_never_moved` still expects `["keep", "keep"]` — the imageharbor keep now carries a `note`; the assertion on actions still holds.
- Any cli test that previously synced a project whose `want` differs from its granted band port (want blocked at grant time, moved into band) will now emit a warning line and exit 1 where it exited 0. If such a test exists, the *test's* expectation changes only if the want genuinely remains blocked; read the failure and update the expectation to match the documented contract, not the other way round.

- [ ] **Step 6: Write the ADR** — create `docs/adr/0014-want-edits-move-an-uncontended-lease.md` (Nygard format; copy `docs/adr/template.md`):

- Title: "14. An edited want moves an uncontended lease"
- Status: Accepted.
- Context: HARBOR_PORTS.md (template v3, dropped into every participating project) documents "edit want, run sync" as the way to change a port, and promises the operator is moved or told why not. The allocator's rule 1 short-circuited on an existing uncontended lease before reading `want`, so the documented workflow was a silent no-op — the operator believed the port moved when nothing did.
- Decision: rule 1 now honours a `want` differing from the held port when the wanted key is free (no lease, no promise, no listener): the decision is a `reassign`, which `sync --new-only` withholds, so the scheduled timer still never renumbers. A blocked want keeps the lease and emits a warning (`Decision.note`), exiting non-zero like compose-default warnings.
- Consequences: a project moved off its preference because the port was held will migrate to it on the first *manual* sync after the port frees — that is the documented semantics of `want`, and the timer's `--new-only` keeps it out of automated runs. "The incumbent always wins" and "a stopped service keeps its port" are unchanged: a want is only honoured when nothing holds the key.

- [ ] **Step 7: Run the whole suite**

Run: `uv run pytest`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/harbor_console/ports/allocate.py src/harbor_console/ports/cli.py tests/test_ports_allocate.py tests/test_ports_cli.py docs/adr/0014-want-edits-move-an-uncontended-lease.md
git commit -m "fix(ports): honor an edited want, or say why not (ADR 14)"
```
