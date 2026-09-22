# Memory Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bare `Memory utilization 13.0%` on both surfaces with `4.1 / 31.3 GiB (13.0%)` plus a swap line, and give `ui.py` its first tests.

**Architecture:** The collector formats the summaries, following the existing `format_uptime` precedent, and both renderers print what they are given. Three small helpers in `system.py` — `format_bytes`, `format_usage`, `get_swap_summary` — replace the single `memory_utilization` key with `memory_summary` and `swap_summary`.

**Tech Stack:** Python 3.13+, `uv`, `psutil`, `rich` (terminal), stdlib `http.server` (web), `pytest`. No new dependency.

## Global Constraints

- Run everything with `uv`: `uv run pytest`. Never `pip`, never venv activation.
- `pyproject.toml` sets `pythonpath = ["src"]`; tests import `harbor_console` with no editable install.
- **Main is always deployable.** Every task leaves the working tree runnable, which is why the old key is removed last, in Task 4, rather than first.
- Collectors never raise on a hostile environment. `get_swap_summary` degrades; the existing unguarded `virtual_memory()` call stays unguarded — guarding it is a question about the whole collector, not about memory.
- Percentages are `.1f` everywhere, so `(13.0%)` not `(13%)`, matching the existing CPU and Disk rows.
- Gibibytes, 1024³, one decimal, as `free -h` counts them. Used is `total - available`, the basis psutil's own `.percent` uses.
- A host with no swap reads `none configured`. `psutil.swap_memory()` raising reads `unavailable`.
- No colors, no bars, no thresholds, no alerting, no configuration. The console stays a table of facts.
- Do not chain shell commands with `&&`; run them as separate calls.
- Full spec: `docs/superpowers/specs/2026-09-22-memory-display-design.md`.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/harbor_console/system.py` | collect metrics, format for display | 3 helpers, 2 new keys, 1 key removed |
| `src/harbor_console/ui.py` | render the terminal dashboard | two rows where one was |
| `src/harbor_console/web.py` | render the HTML page | two rows, magic index removed |
| `src/harbor_console/webapp.py` | coordinate; holds the pre-first-cycle metrics | two `"collecting"` placeholders |
| `tests/test_system.py` | | helpers, degradation, updated contract |
| `tests/test_ui.py` | | **create** — the module's first coverage |
| `tests/test_web.py` | | host table rows, tailnet position |
| `tests/test_webapp.py` | | fixture and starting-snapshot keys |

---

### Task 1: The collector formats memory and swap

**Files:**
- Modify: `src/harbor_console/system.py`
- Test: `tests/test_system.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `format_bytes(n: int) -> str` — gibibytes, one decimal, no unit (`"4.1"`).
  - `format_usage(used: int, total: int, percent: float) -> str` — `"4.1 / 31.3 GiB (13.0%)"`.
  - `get_swap_summary(swap_memory: Callable[[], object] = psutil.swap_memory) -> str`.
  - `collect_system_metrics()` gains `"memory_summary"` and `"swap_summary"`. It still returns `"memory_utilization"` — Task 4 removes that, once no renderer reads it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_system.py`:

```python
def test_format_bytes_counts_gibibytes():
    assert system.format_bytes(1024**3) == "1.0"
    assert system.format_bytes(0) == "0.0"
    assert system.format_bytes(1536 * 1024**2) == "1.5"


def test_format_usage_pairs_the_bytes_with_the_percent():
    assert system.format_usage(4 * 1024**3, 32 * 1024**3, 12.97) == "4.0 / 32.0 GiB (13.0%)"


def test_swap_summary_reports_used_of_total():
    swap = SimpleNamespace(total=8 * 1024**3, used=2 * 1024**3, percent=25.0)

    assert system.get_swap_summary(swap_memory=lambda: swap) == "2.0 / 8.0 GiB (25.0%)"


def test_swap_summary_says_none_configured_when_there_is_no_swap():
    swap = SimpleNamespace(total=0, used=0, percent=0.0)

    assert system.get_swap_summary(swap_memory=lambda: swap) == "none configured"


def test_swap_summary_degrades_when_psutil_cannot_answer():
    def boom():
        raise RuntimeError("no swap on this platform")

    assert system.get_swap_summary(swap_memory=boom) == "unavailable"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_system.py -v`
Expected: FAIL — `AttributeError: module 'harbor_console.system' has no attribute 'format_bytes'`.

- [ ] **Step 3: Write the three helpers**

In `src/harbor_console/system.py`, add below `format_uptime`:

```python
def format_bytes(n: int) -> str:
    """Gibibytes with one decimal, as `free -h` counts them.

    Returns the number alone, without a unit: a summary line names `GiB` once
    for the pair rather than twice.
    """
    return f"{n / 1024**3:.1f}"


def format_usage(used: int, total: int, percent: float) -> str:
    """`used / total GiB (percent)` -- the shape both memory and swap take."""
    return f"{format_bytes(used)} / {format_bytes(total)} GiB ({percent:.1f}%)"


def get_swap_summary(
    swap_memory: Callable[[], object] = psutil.swap_memory,
) -> str:
    """Swap usage, degrading rather than raising.

    `psutil.swap_memory()` can fail outright on some platforms, and a
    collector here never raises on a hostile environment. A host with no swap
    says so rather than reading `0.0 / 0.0 GiB (0.0%)`, which looks like a bug
    rather than a fact.
    """
    try:
        swap = swap_memory()
        total = int(swap.total)  # type: ignore[attr-defined]
        used = int(swap.used)  # type: ignore[attr-defined]
        percent = float(swap.percent)  # type: ignore[attr-defined]
    except Exception:
        return "unavailable"
    if total == 0:
        return "none configured"
    return format_usage(used, total, percent)
```

Check the file's imports for `Callable`; if `from collections.abc import Callable` is not already there, add it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_system.py -v`
Expected: PASS for the five new tests. `test_collect_system_metrics` still passes — the dict has not changed yet.

- [ ] **Step 5: Write the failing contract test**

Replace `test_collect_system_metrics` in `tests/test_system.py`. The existing fake for `virtual_memory` returns only `percent`; the collector now needs `total` and `available` as well:

```python
def test_collect_system_metrics(monkeypatch):
    fake_now = SimpleNamespace(
        timestamp=lambda: 1_000.0,
        strftime=lambda _fmt: "2026-08-01 00:00:00",
    )
    memory = SimpleNamespace(
        percent=12.5, total=32 * 1024**3, available=28 * 1024**3
    )

    monkeypatch.setattr(system, "datetime", SimpleNamespace(now=lambda: fake_now))
    monkeypatch.setattr(system.psutil, "boot_time", lambda: 900.0)
    monkeypatch.setattr(system.psutil, "cpu_percent", lambda interval=None: 12.5)
    monkeypatch.setattr(system.psutil, "virtual_memory", lambda: memory)
    monkeypatch.setattr(system.psutil, "disk_usage", lambda _path: SimpleNamespace(percent=78.0))
    monkeypatch.setattr(system.socket, "gethostname", lambda: "host-a")
    monkeypatch.setattr(system, "get_ipv4_address", lambda: "10.0.0.7")
    monkeypatch.setattr(system, "get_docker_container_count", lambda: 3)
    monkeypatch.setattr(system, "get_swap_summary", lambda: "0.0 / 8.0 GiB (0.0%)")

    metrics = system.collect_system_metrics()

    assert metrics == {
        "hostname": "host-a",
        "uptime": "0d 00:01:40",
        "cpu_utilization": 12.5,
        "memory_utilization": 12.5,
        "memory_summary": "4.0 / 32.0 GiB (12.5%)",
        "swap_summary": "0.0 / 8.0 GiB (0.0%)",
        "disk_utilization": 78.0,
        "ipv4_address": "10.0.0.7",
        "docker_container_count": 3,
        "current_datetime": "2026-08-01 00:00:00",
    }
```

- [ ] **Step 6: Run it to verify it fails**

Run: `uv run pytest tests/test_system.py::test_collect_system_metrics -v`
Expected: FAIL — the returned dict has neither `memory_summary` nor `swap_summary`.

- [ ] **Step 7: Add the keys to the collector**

In `collect_system_metrics()`, bind the memory reading once (it is read three times now) and add the two keys:

```python
def collect_system_metrics() -> dict[str, str | float | int]:
    """Collect all metrics required for Harbor Console MVP."""
    now = datetime.now()
    uptime_seconds = int(now.timestamp() - psutil.boot_time())
    memory = psutil.virtual_memory()

    return {
        "hostname": socket.gethostname(),
        "uptime": format_uptime(uptime_seconds),
        "cpu_utilization": psutil.cpu_percent(interval=None),
        "memory_utilization": memory.percent,
        # Used is total - available, the basis psutil's own `percent` uses, so
        # the bytes and the percentage on one line agree with each other.
        "memory_summary": format_usage(
            memory.total - memory.available, memory.total, memory.percent
        ),
        "swap_summary": get_swap_summary(),
        "disk_utilization": psutil.disk_usage("/").percent,
        "ipv4_address": get_ipv4_address(),
        "docker_container_count": get_docker_container_count(),
        "current_datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
```

- [ ] **Step 8: Run the whole suite**

Run: `uv run pytest`
Expected: PASS. Nothing reads the new keys yet, and `memory_utilization` is still there for the two renderers.

- [ ] **Step 9: Commit**

```bash
git add src/harbor_console/system.py tests/test_system.py
git commit -m "feat(system): summarise memory and swap with scale, not a bare percent"
```

---

### Task 2: The terminal dashboard shows both lines, and gets its first tests

**Files:**
- Modify: `src/harbor_console/ui.py:18`
- Test: `tests/test_ui.py` (**create**)

**Interfaces:**
- Consumes: `metrics["memory_summary"]` and `metrics["swap_summary"]` from Task 1.
- Produces: no signature change. `build_dashboard(metrics)` still returns a `rich.panel.Panel`.

`ui.py` currently has no tests at all — `build_dashboard` is referenced only by `app.py` and itself. This task is its first coverage, and the last test below is the one CLAUDE.md describes as the contract between `system` and `ui` while nothing enforces it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ui.py`:

```python
from rich.console import Console

import harbor_console.system as system
from harbor_console.ui import build_dashboard

METRICS = {
    "hostname": "host-a",
    "uptime": "0d 00:01:40",
    "cpu_utilization": 12.5,
    "memory_summary": "4.0 / 32.0 GiB (12.5%)",
    "swap_summary": "0.0 / 8.0 GiB (0.0%)",
    "disk_utilization": 78.0,
    "ipv4_address": "10.0.0.7",
    "docker_container_count": 3,
    "current_datetime": "2026-08-01 00:00:00",
}


def render(metrics):
    """The panel as text. Wide enough that no cell wraps."""
    console = Console(width=120, record=True)
    console.print(build_dashboard(metrics))
    return console.export_text()


def test_dashboard_shows_every_metric():
    page = render(METRICS)

    assert "host-a" in page
    assert "0d 00:01:40" in page
    assert "12.5%" in page
    assert "78.0%" in page
    assert "10.0.0.7" in page
    assert "2026-08-01 00:00:00" in page


def test_dashboard_shows_memory_with_its_scale():
    page = render(METRICS)

    assert "4.0 / 32.0 GiB (12.5%)" in page


def test_dashboard_shows_swap_on_its_own_row():
    page = render(METRICS)

    assert "Swap" in page
    assert "0.0 / 8.0 GiB (0.0%)" in page


def test_dashboard_reports_a_host_with_no_swap():
    page = render(dict(METRICS, swap_summary="none configured"))

    assert "none configured" in page


def test_the_renderer_and_the_collector_agree_on_every_key(monkeypatch):
    """The contract CLAUDE.md describes, enforced.

    The two collectors that reach outside this process are stubbed -- one
    shells out to `docker`, the other opens a socket -- because tests here use
    neither. Everything else is the real collector, so a key renamed on one
    side and not the other fails here with a KeyError.
    """
    monkeypatch.setattr(system, "get_docker_container_count", lambda: 0)
    monkeypatch.setattr(system, "get_ipv4_address", lambda: "127.0.0.1")

    render(system.collect_system_metrics())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ui.py -v`
Expected: FAIL — `KeyError: 'memory_summary'` from `build_dashboard`, on every test including the contract one.

- [ ] **Step 3: Render both rows**

In `src/harbor_console/ui.py`, replace the single memory row:

```python
    table.add_row("CPU utilization", f"{float(metrics['cpu_utilization']):.1f}%")
    table.add_row("Memory", str(metrics["memory_summary"]))
    table.add_row("Swap", str(metrics["swap_summary"]))
    table.add_row("Disk utilization", f"{float(metrics['disk_utilization']):.1f}%")
```

These two labels drop the word "utilization" because the value is no longer a bare percentage. `CPU utilization` and `Disk utilization` keep theirs.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ui.py -v`
Expected: PASS, all five.

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_console/ui.py tests/test_ui.py
git commit -m "feat(ui): memory with its scale, swap on its own row, and first tests"
```

---

### Task 3: The web page shows both lines, and loses a magic index

**Files:**
- Modify: `src/harbor_console/web.py:74-89` (`_host_table`), `src/harbor_console/webapp.py:75-90` (`starting_snapshot`)
- Test: `tests/test_web.py`, `tests/test_webapp.py`

**Interfaces:**
- Consumes: `metrics["memory_summary"]` and `metrics["swap_summary"]` from Task 1.
- Produces: no signature change to either function.

Two things happen here, and the second is why they are one task. `_host_table` builds its row list and then places the tailnet row by index:

```python
    if snapshot.tailnet_address is not None:
        rows.insert(5, ("Tailnet", snapshot.tailnet_address))
```

Index 5 currently lands between IPv4 and Containers. Adding a Swap row above it moves that silently to between Disk and IPv4, and no test pins where it sits. And `starting_snapshot` in `webapp.py` hardcodes the whole metrics dict for the page's first answer before the prober's first cycle — without the new keys, the very first request raises `KeyError`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_web.py`, update the `METRICS` fixture — drop `memory_utilization`, which nothing will read after this task, and add the two summaries:

```python
METRICS = {
    "hostname": "hpz440",
    "uptime": "1d 00:00:00",
    "cpu_utilization": 1.0,
    "memory_summary": "4.0 / 32.0 GiB (12.5%)",
    "swap_summary": "0.0 / 8.0 GiB (0.0%)",
    "disk_utilization": 3.0,
    "ipv4_address": "10.0.0.7",
    "docker_container_count": 1,
    "current_datetime": "2026-09-02 14:02:11",
}
```

and append the tests:

```python
def test_host_table_shows_memory_with_its_scale():
    page = web.render_page(snapshot()).decode()

    assert "4.0 / 32.0 GiB (12.5%)" in page


def test_host_table_shows_swap():
    page = web.render_page(snapshot()).decode()

    assert "Swap" in page
    assert "0.0 / 8.0 GiB (0.0%)" in page


def test_the_tailnet_row_sits_between_ipv4_and_containers():
    page = web.render_page(snapshot()).decode()

    assert page.index("IPv4") < page.index("Tailnet") < page.index("Containers")


def test_the_host_table_omits_the_tailnet_row_without_an_address():
    page = web.render_page(snapshot(tailnet_address=None)).decode()

    assert "Tailnet" not in page
```

In `tests/test_webapp.py`, update its `METRICS` fixture the same way (drop `memory_utilization`, add both summaries), and append:

```python
def test_the_starting_snapshot_has_every_key_the_page_renders():
    snapshot = webapp.starting_snapshot("hpz440", NOW, tailnet_address="100.69.239.123")

    web.render_page(snapshot)
```

Add `from harbor_console import web` to that file's imports if it is not already there.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web.py tests/test_webapp.py -v`
Expected: FAIL — `KeyError: 'memory_utilization'` from `_host_table` (the fixture no longer supplies it), and the starting-snapshot test failing with `KeyError: 'memory_summary'`.

- [ ] **Step 3: Rebuild the host table without the magic index**

In `src/harbor_console/web.py`, replace the body of `_host_table` up to the `cells = ` line:

```python
def _host_table(snapshot: Snapshot) -> str:
    rows = [
        ("Uptime", snapshot.metrics["uptime"]),
        ("CPU", f"{float(snapshot.metrics['cpu_utilization']):.1f}%"),
        ("Memory", snapshot.metrics["memory_summary"]),
        ("Swap", snapshot.metrics["swap_summary"]),
        ("Disk", f"{float(snapshot.metrics['disk_utilization']):.1f}%"),
        ("IPv4", snapshot.metrics["ipv4_address"]),
    ]
    # Placed here rather than inserted by index: a row added above would move
    # it silently, and this is the address the whole page is served on.
    if snapshot.tailnet_address is not None:
        rows.append(("Tailnet", snapshot.tailnet_address))
    rows.append(("Containers", snapshot.metrics["docker_container_count"]))
    rows.append(("Time", snapshot.metrics["current_datetime"]))
    cells = "".join(
        f"<tr><td>{escape(label)}</td><td>{escape(str(value))}</td></tr>"
        for label, value in rows
    )
    return f"<h2>Host</h2><table>{cells}</table>"
```

The row order is exactly what it was, with Swap added after Memory.

- [ ] **Step 4: Give the starting snapshot the new keys**

In `src/harbor_console/webapp.py`, inside `starting_snapshot`, replace the `memory_utilization` line with the two summaries, using the same `"collecting"` idiom the uptime line already uses:

```python
            "cpu_utilization": 0.0,
            "memory_summary": "collecting",
            "swap_summary": "collecting",
            "disk_utilization": 0.0,
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web.py tests/test_webapp.py -v`
Expected: PASS, both files.

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/harbor_console/web.py src/harbor_console/webapp.py tests/test_web.py tests/test_webapp.py
git commit -m "feat(web): memory with its scale and a swap row, tailnet row no longer placed by index"
```

---

### Task 4: Remove the key nothing reads

**Files:**
- Modify: `src/harbor_console/system.py`
- Test: `tests/test_system.py`

**Interfaces:**
- Consumes: the renderers from Tasks 2 and 3, which no longer read `memory_utilization`.
- Produces: `collect_system_metrics()` returns nine keys, without `memory_utilization`.

This is last so that every earlier commit leaves a runnable tree.

- [ ] **Step 1: Confirm nothing reads it**

Run: `grep -rn "memory_utilization" --include=*.py src/ tests/`
Expected: two hits only — `src/harbor_console/system.py` (the key itself) and `tests/test_system.py` (the expected dict). If anything else appears, stop and report it rather than removing the key.

Historical plans under `docs/superpowers/plans/` also mention it. Leave them alone: they are the record of what was built at the time, not a description of the code.

- [ ] **Step 2: Write the failing test**

In `tests/test_system.py`, remove the `"memory_utilization": 12.5,` line from `test_collect_system_metrics`'s expected dict.

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run pytest tests/test_system.py::test_collect_system_metrics -v`
Expected: FAIL — the returned dict still carries `memory_utilization`, so the comparison differs by that one key.

- [ ] **Step 4: Remove the key**

In `src/harbor_console/system.py`, delete the `"memory_utilization": memory.percent,` line from the returned dict. `memory.percent` is still used by the `memory_summary` line above it, so the local binding stays.

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_console/system.py tests/test_system.py
git commit -m "refactor(system): drop memory_utilization, superseded by memory_summary"
```

---

## Verification

The tests cover the formatting; only the host shows whether the numbers are right.

- [ ] `uv run pytest` — whole suite green.
- [ ] `ssh gte@hpz440 "free -h"` — keep the output; it is the oracle.
- [ ] Deploy: `ssh conrad@hpz440` (the account that can sudo), `git pull` in `/srv/harbor-console`, then `sudo bash install.sh`. That step needs a terminal for the sudo password, so it is the user's to run.
- [ ] Read `https://harbor.hpz440.ohr3023.org/`. The Memory row's total matches `free -h`'s total, its used figure is within ~0.2 GiB of `free`'s used column, and the percentage matches used/total. The Swap row shows the same total `free` reports.
- [ ] `ssh conrad@hpz440 "systemctl restart harbor-console"`, then look at tty1 (or `systemctl status harbor-console`): the terminal dashboard shows Memory and Swap rows, and no row reads `collecting` once a cycle has run.
