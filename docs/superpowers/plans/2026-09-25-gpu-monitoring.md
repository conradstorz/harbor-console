# GPU Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One row per GPU on both surfaces, read from `/sys/class/drm`, with every metric optional and a visible "none detected" when the host has no card.

**Architecture:** A new collector module `gpu.py` shaped exactly like `storage.py` (dataclass entry, `collect_*` returning a tuple, `format_*` for the string). It is injected into `app.run` and `webapp.collect_snapshot` the same way `collect_storage` is, carried on `Snapshot.gpus`, and rendered by `ui.build_dashboard` and a `_gpu_section` in `web.py`. No subprocess, no timeout: sysfs reads cannot hang.

**Tech Stack:** Python 3.13, `pathlib`, `rich` (existing), stdlib only. `uv run pytest`.

Spec: `docs/superpowers/specs/2026-09-25-gpu-monitoring-design.md`.

## Global Constraints

- Collectors never raise on a hostile environment. Missing, unreadable or malformed sysfs files yield `None` for that field; an unlistable root yields one `unavailable` entry (ADR 18: nothing found and nothing looked at must not render alike).
- Collect / render / coordinate stay in separate modules. `gpu.py` renders nothing; `ui.py` and `web.py` collect nothing.
- Stdlib `http.server` only, no new dependency.
- Tests use no real sysfs: every tree lives under `tmp_path` via the injectable `drm_root`. No symlinks in test trees (Windows dev box) -- the driver name comes from the `DRIVER=` line of `device/uevent`.
- Every `web.py` cell that originates outside this project goes through `html.escape`.
- Run the whole suite with `uv run pytest`; every task ends green.
- Do not chain shell commands with `&&`; run them as separate calls.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Branch: `feat/gpu-monitoring` (already exists, spec committed on it).

---

## File map

| File | Responsibility |
|---|---|
| `src/harbor_console/gpu.py` (create) | `GpuEntry`, `NOTE_UNAVAILABLE`, `collect_gpus(drm_root)`, `format_gpu(entry)` |
| `tests/test_gpu.py` (create) | collector and formatter tests on `tmp_path` trees |
| `src/harbor_console/ui.py` (modify) | `build_dashboard(metrics, storage=(), gpus=())`, GPU rows after storage |
| `tests/test_ui.py` (modify) | GPU row tests |
| `src/harbor_console/app.py` (modify) | `gpu_collector` parameter, renderer gets a third argument |
| `tests/test_app.py` (modify) | existing renderer fakes take three args; new injection test |
| `src/harbor_console/snapshot.py` (modify) | `gpus: tuple[GpuEntry, ...] = ()` |
| `src/harbor_console/webapp.py` (modify) | `gpus` parameter on `collect_snapshot` |
| `tests/test_webapp.py` (modify) | injection and default tests |
| `src/harbor_console/web.py` (modify) | `_gpu_section`, third cell in `.resource-grid` |
| `tests/test_web.py` (modify) | section tests, escape test extended |
| `CLAUDE.md` (modify) | describe `gpu.py` next to `storage.py` |

---

### Task 1: `GpuEntry` and `format_gpu`

**Files:**
- Create: `src/harbor_console/gpu.py`
- Create: `tests/test_gpu.py`

**Interfaces:**
- Consumes: `harbor_console.system.format_usage(used: int, total: int, percent: float) -> str` (existing; renders `1.0 / 8.0 GiB (12.5%)`).
- Produces:
  ```python
  @dataclass(frozen=True)
  class GpuEntry:
      label: str
      driver: str = ""
      busy_percent: int | None = None
      vram_used: int | None = None
      vram_total: int | None = None
      temp_c: float | None = None
      note: str = ""

  NOTE_UNAVAILABLE = "unavailable"
  def format_gpu(entry: GpuEntry) -> str: ...
  ```

- [ ] **Step 1: Write the failing tests**

`tests/test_gpu.py`:

```python
from harbor_console.gpu import NOTE_UNAVAILABLE, GpuEntry, format_gpu


def test_format_joins_every_present_field_in_order():
    entry = GpuEntry(
        label="GPU card0 (amdgpu)",
        driver="amdgpu",
        busy_percent=12,
        vram_used=1 * 1024**3,
        vram_total=8 * 1024**3,
        temp_c=54.0,
    )

    assert format_gpu(entry) == "busy 12% · VRAM 1.0 / 8.0 GiB (12.5%) · 54 °C"


def test_format_shows_only_what_the_driver_exposed():
    entry = GpuEntry(label="GPU card0 (radeon)", driver="radeon", temp_c=35.0)

    assert format_gpu(entry) == "35 °C"


def test_format_omits_vram_when_only_half_of_the_pair_is_known():
    entry = GpuEntry(label="GPU card0 (amdgpu)", driver="amdgpu", vram_used=5, busy_percent=3)

    assert format_gpu(entry) == "busy 3%"


def test_format_omits_vram_when_total_is_zero():
    entry = GpuEntry(label="GPU card0 (amdgpu)", driver="amdgpu", vram_used=0, vram_total=0)

    assert format_gpu(entry) == "no metrics exposed by amdgpu"


def test_format_names_the_driver_when_nothing_was_exposed():
    assert format_gpu(GpuEntry(label="GPU card0 (radeon)", driver="radeon")) == (
        "no metrics exposed by radeon"
    )


def test_format_with_no_driver_and_nothing_exposed():
    assert format_gpu(GpuEntry(label="GPU card0")) == "no metrics exposed"


def test_format_renders_a_note_alone():
    assert format_gpu(GpuEntry(label="GPU", note=NOTE_UNAVAILABLE)) == "unavailable"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_gpu.py -v`
Expected: FAIL at import, `ModuleNotFoundError: No module named 'harbor_console.gpu'`.

- [ ] **Step 3: Write the module with the entry and formatter**

`src/harbor_console/gpu.py`:

```python
"""What GPUs this host has, from sysfs.

Collects only. One entry per `/sys/class/drm/card<N>`, every metric optional:
a driver exposes what it exposes, and the entry carries a number where one
exists and nothing where none does, so a renderer never decides which driver
it is looking at. On hpz440 the `radeon` driver exposes a temperature and
nothing else; `amdgpu` adds busy percent and VRAM.

Reads files and runs no subprocess, so nothing here can hang and nothing
needs a timeout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from harbor_console.system import format_usage

NOTE_UNAVAILABLE = "unavailable"

#: `card0` is a card; `card0-DP-1` is one of its connectors, `renderD128` a
#: render node, `version` a file. Only the first is a GPU.
_CARD = re.compile(r"^card(\d+)$")


@dataclass(frozen=True)
class GpuEntry:
    """One GPU, with whatever its driver chose to say about it."""

    label: str
    driver: str = ""
    busy_percent: int | None = None
    #: Bytes. Rendered only when both are known and total is non-zero.
    vram_used: int | None = None
    vram_total: int | None = None
    temp_c: float | None = None
    note: str = ""


def format_gpu(entry: GpuEntry) -> str:
    """The three shapes an entry renders as, and nothing else.

    Present fields joined in a fixed order; nothing present names the driver
    that stayed silent, so a bare row still says why; a note stands alone.
    """
    if entry.note:
        return entry.note
    parts: list[str] = []
    if entry.busy_percent is not None:
        parts.append(f"busy {entry.busy_percent}%")
    if entry.vram_used is not None and entry.vram_total:
        percent = 100.0 * entry.vram_used / entry.vram_total
        parts.append(f"VRAM {format_usage(entry.vram_used, entry.vram_total, percent)}")
    if entry.temp_c is not None:
        parts.append(f"{entry.temp_c:.0f} °C")
    if parts:
        return " · ".join(parts)
    if entry.driver:
        return f"no metrics exposed by {entry.driver}"
    return "no metrics exposed"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_gpu.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/gpu.py tests/test_gpu.py
git commit -m "gpu: entry dataclass and formatter

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `collect_gpus` from a sysfs tree

**Files:**
- Modify: `src/harbor_console/gpu.py`
- Modify: `tests/test_gpu.py`

**Interfaces:**
- Consumes: `GpuEntry`, `NOTE_UNAVAILABLE`, `_CARD` from Task 1.
- Produces: `collect_gpus(drm_root: str | Path = "/sys/class/drm") -> tuple[GpuEntry, ...]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_gpu.py` (add `collect_gpus` to the import line, and `from pathlib import Path` at the top):

```python
def card(root: Path, name: str, driver: str | None = "radeon", **files: str) -> Path:
    """Build `<root>/<name>/device/...` the way sysfs lays it out.

    `files` are written relative to `device/`; a key with `__` in it is a
    nested path (`hwmon__hwmon2__temp1_input`). `driver=None` writes no
    `uevent` at all.
    """
    device = root / name / "device"
    device.mkdir(parents=True)
    if driver is not None:
        (device / "uevent").write_text(f"DRIVER={driver}\nPCI_ID=1002:6610\n")
    for key, value in files.items():
        target = device.joinpath(*key.split("__"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)
    return device


def test_collect_reads_a_radeon_card_with_only_a_temperature(tmp_path):
    card(tmp_path, "card0", hwmon__hwmon2__temp1_input="35000\n")

    (entry,) = collect_gpus(tmp_path)

    assert entry == GpuEntry(label="GPU card0 (radeon)", driver="radeon", temp_c=35.0)


def test_collect_reads_every_amdgpu_metric(tmp_path):
    card(
        tmp_path,
        "card0",
        driver="amdgpu",
        gpu_busy_percent="12\n",
        mem_info_vram_used=str(1024**3),
        mem_info_vram_total=str(8 * 1024**3),
        hwmon__hwmon0__temp1_input="54000",
    )

    (entry,) = collect_gpus(tmp_path)

    assert entry == GpuEntry(
        label="GPU card0 (amdgpu)",
        driver="amdgpu",
        busy_percent=12,
        vram_used=1024**3,
        vram_total=8 * 1024**3,
        temp_c=54.0,
    )


def test_collect_ignores_connectors_render_nodes_and_files(tmp_path):
    card(tmp_path, "card0")
    (tmp_path / "card0-DP-1").mkdir()
    (tmp_path / "card0-DVI-I-1").mkdir()
    (tmp_path / "renderD128").mkdir()
    (tmp_path / "version").write_text("drm 1.1.0 20060810\n")

    entries = collect_gpus(tmp_path)

    assert [e.label for e in entries] == ["GPU card0 (radeon)"]


def test_collect_sorts_cards_by_number(tmp_path):
    card(tmp_path, "card10", driver="amdgpu")
    card(tmp_path, "card2", driver="nouveau")
    card(tmp_path, "card0")

    assert [e.label for e in collect_gpus(tmp_path)] == [
        "GPU card0 (radeon)",
        "GPU card2 (nouveau)",
        "GPU card10 (amdgpu)",
    ]


def test_collect_keeps_a_card_whose_driver_exposes_nothing(tmp_path):
    card(tmp_path, "card0")

    (entry,) = collect_gpus(tmp_path)

    assert entry == GpuEntry(label="GPU card0 (radeon)", driver="radeon")
    assert format_gpu(entry) == "no metrics exposed by radeon"


def test_collect_labels_a_card_bare_when_uevent_is_missing(tmp_path):
    card(tmp_path, "card0", driver=None)

    (entry,) = collect_gpus(tmp_path)

    assert entry == GpuEntry(label="GPU card0")


def test_collect_labels_a_card_bare_when_uevent_has_no_driver_line(tmp_path):
    device = card(tmp_path, "card0", driver=None)
    (device / "uevent").write_text("PCI_ID=1002:6610\n")

    (entry,) = collect_gpus(tmp_path)

    assert entry.label == "GPU card0"
    assert entry.driver == ""


def test_collect_drops_only_the_field_that_is_malformed(tmp_path):
    card(
        tmp_path,
        "card0",
        driver="amdgpu",
        gpu_busy_percent="garbage",
        hwmon__hwmon0__temp1_input="41000",
    )

    (entry,) = collect_gpus(tmp_path)

    assert entry.busy_percent is None
    assert entry.temp_c == 41.0


def test_collect_reports_unavailable_when_the_root_cannot_be_listed(tmp_path):
    assert collect_gpus(tmp_path / "missing") == (GpuEntry(label="GPU", note=NOTE_UNAVAILABLE),)


def test_collect_returns_nothing_for_a_host_with_no_cards(tmp_path):
    (tmp_path / "version").write_text("drm 1.1.0\n")

    assert collect_gpus(tmp_path) == ()


def test_collect_defaults_to_the_real_sysfs_root():
    import inspect

    assert inspect.signature(collect_gpus).parameters["drm_root"].default == "/sys/class/drm"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_gpu.py -v`
Expected: `ImportError: cannot import name 'collect_gpus'`.

- [ ] **Step 3: Implement `collect_gpus`**

Append to `src/harbor_console/gpu.py`:

```python
def _read_int(path: Path) -> int | None:
    """One sysfs number, or `None` for a file that is missing, unreadable or
    not a number. Guarded per file: one bad value costs only itself."""
    try:
        return int(path.read_text(encoding="ascii", errors="replace").strip())
    except (OSError, ValueError):
        return None


def _driver(device: Path) -> str:
    """The `DRIVER=` line of `device/uevent`, or "" when there is none.

    `uevent` is a regular file where `device/driver` is a symlink; the same
    name, without needing a test tree to hold a link.
    """
    try:
        text = (device / "uevent").read_text(encoding="ascii", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "DRIVER":
            return value.strip()
    return ""


def _temperature(device: Path) -> float | None:
    """The first hwmon `temp1_input` under the device, in degrees Celsius.

    sysfs reports millidegrees. Guarded on the listing and on the read, so a
    device with no `hwmon` directory simply has no temperature.
    """
    try:
        sensors = sorted((device / "hwmon").iterdir())
    except OSError:
        return None
    for sensor in sensors:
        millidegrees = _read_int(sensor / "temp1_input")
        if millidegrees is not None:
            return millidegrees / 1000.0
    return None


def _card_entry(node: Path) -> GpuEntry:
    device = node / "device"
    driver = _driver(device)
    label = f"GPU {node.name} ({driver})" if driver else f"GPU {node.name}"
    return GpuEntry(
        label=label,
        driver=driver,
        busy_percent=_read_int(device / "gpu_busy_percent"),
        vram_used=_read_int(device / "mem_info_vram_used"),
        vram_total=_read_int(device / "mem_info_vram_total"),
        temp_c=_temperature(device),
    )


def collect_gpus(drm_root: str | Path = "/sys/class/drm") -> tuple[GpuEntry, ...]:
    """One entry per card under `drm_root`, in card-number order.

    A root that cannot be listed yields one `unavailable` entry rather than an
    empty tuple -- nothing found and nothing looked at must not render alike
    (ADR 18). A root with no cards yields an empty tuple; the renderers own
    the "none detected" wording.
    """
    try:
        names = [p.name for p in Path(drm_root).iterdir()]
    except OSError:
        return (GpuEntry(label="GPU", note=NOTE_UNAVAILABLE),)

    cards: list[tuple[int, str]] = []
    for name in names:
        match = _CARD.match(name)
        if match:
            cards.append((int(match.group(1)), name))
    cards.sort()
    return tuple(_card_entry(Path(drm_root) / name) for _, name in cards)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_gpu.py -v`
Expected: 18 passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/gpu.py tests/test_gpu.py
git commit -m "gpu: collect one entry per DRM card from sysfs

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Dashboard rows

**Files:**
- Modify: `src/harbor_console/ui.py`
- Modify: `tests/test_ui.py`

**Interfaces:**
- Consumes: `GpuEntry`, `format_gpu` from `harbor_console.gpu`.
- Produces: `build_dashboard(metrics, storage=(), gpus: tuple[GpuEntry, ...] = ()) -> Panel`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_ui.py`, add `from harbor_console.gpu import GpuEntry` after the `StorageEntry` import, change the `render` helper to take a third argument, and append two tests:

```python
def render(metrics, storage=(), gpus=()):
    """The panel as text. Wide enough that no cell wraps."""
    console = Console(width=120, record=True)
    console.print(build_dashboard(metrics, storage, gpus))
    return console.export_text()
```

```python
def test_dashboard_shows_one_row_per_gpu():
    gpus = (
        GpuEntry(label="GPU card0 (radeon)", driver="radeon", temp_c=35.0),
        GpuEntry(label="GPU card1 (amdgpu)", driver="amdgpu", busy_percent=7),
    )

    lines = render(METRICS, (), gpus).splitlines()

    assert any("GPU card0 (radeon)" in line and "35 °C" in line for line in lines)
    assert any("GPU card1 (amdgpu)" in line and "busy 7%" in line for line in lines)


def test_dashboard_says_none_detected_when_there_are_no_gpus():
    lines = render(METRICS).splitlines()

    assert any(line.split()[1:2] == ["GPU"] and "none detected" in line for line in lines)


def test_dashboard_puts_gpu_rows_between_storage_and_ipv4():
    storage = (StorageEntry(label="VG ubuntu-vg", total=251111931904, note="unallocated"),)
    gpus = (GpuEntry(label="GPU card0 (radeon)", driver="radeon", temp_c=35.0),)

    page = render(METRICS, storage, gpus)

    assert page.index("VG ubuntu-vg") < page.index("GPU card0") < page.index("IPv4 address")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ui.py -v`
Expected: the three new tests FAIL (`TypeError: build_dashboard() takes from 1 to 2 positional arguments but 3 were given`); the rest pass.

- [ ] **Step 3: Add the rows to the renderer**

`src/harbor_console/ui.py`:

```python
"""UI rendering for Harbor Console."""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table

from harbor_console.gpu import GpuEntry, format_gpu
from harbor_console.storage import StorageEntry, format_entry


def build_dashboard(
    metrics: dict[str, str | float | int],
    storage: tuple[StorageEntry, ...] = (),
    gpus: tuple[GpuEntry, ...] = (),
) -> Panel:
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
    table.add_row("Docker container count", str(metrics["docker_container_count"]))
    table.add_row("Current date/time", str(metrics["current_datetime"]))

    return Panel(table, title="Harbor Console", border_style="white")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ui.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/ui.py tests/test_ui.py
git commit -m "ui: one dashboard row per GPU, none detected when there are none

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Dashboard loop injection

**Files:**
- Modify: `src/harbor_console/app.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- Consumes: `collect_gpus`, `GpuEntry` from `harbor_console.gpu`; `build_dashboard(metrics, storage, gpus)` from Task 3.
- Produces: `app.run(..., gpu_collector: Callable[[], tuple[GpuEntry, ...]] = collect_gpus)`; `DashboardBuilder` is now `Callable[[dict, tuple[StorageEntry, ...], tuple[GpuEntry, ...]], object]`.

- [ ] **Step 1: Update the existing fakes and add the failing test**

In `tests/test_app.py`, the two existing renderer fakes take a third positional argument:

```python
    def renderer(metrics, _storage, _gpus):
        return f"render-{metrics['tick']}"
```

```python
    def renderer(metrics, storage, _gpus):
        seen["storage"] = storage
        return "rendered"
```

Append:

```python
def test_run_passes_gpus_to_the_renderer(monkeypatch):
    seen = {}

    def renderer(metrics, _storage, gpus):
        seen["gpus"] = gpus
        return "rendered"

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "Live", DummyLive)

    result = app.run(
        collector=lambda: {"tick": 1},
        renderer=renderer,
        sleep=fake_sleep,
        storage_collector=lambda: (),
        gpu_collector=lambda: ("gpu",),
    )

    assert result == 0
    assert seen["gpus"] == ("gpu",)


def test_run_defaults_to_the_real_gpu_collector():
    import inspect

    assert inspect.signature(app.run).parameters["gpu_collector"].default is app.collect_gpus
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_app.py -v`
Expected: the two existing tests FAIL (`TypeError: renderer() missing 1 required positional argument: '_gpus'`), the new ones FAIL (`unexpected keyword argument 'gpu_collector'` / `KeyError`).

- [ ] **Step 3: Thread the collector through the loop**

`src/harbor_console/app.py`:

```python
"""Application entrypoint and refresh loop."""

from __future__ import annotations

import time
from collections.abc import Callable

from rich.live import Live

from harbor_console.gpu import GpuEntry, collect_gpus
from harbor_console.storage import StorageEntry, collect_storage
from harbor_console.system import collect_system_metrics
from harbor_console.ui import build_dashboard


MetricsCollector = Callable[[], dict[str, str | float | int]]
StorageCollector = Callable[[], tuple[StorageEntry, ...]]
GpuCollector = Callable[[], tuple[GpuEntry, ...]]
DashboardBuilder = Callable[
    [dict[str, str | float | int], tuple[StorageEntry, ...], tuple[GpuEntry, ...]], object
]


def run(
    refresh_interval: float = 1.0,
    collector: MetricsCollector = collect_system_metrics,
    renderer: DashboardBuilder = build_dashboard,
    sleep: Callable[[float], None] = time.sleep,
    storage_collector: StorageCollector = collect_storage,
    gpu_collector: GpuCollector = collect_gpus,
) -> int:
    """Run the Harbor Console refresh loop."""

    def frame() -> object:
        return renderer(collector(), storage_collector(), gpu_collector())

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

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_app.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/app.py tests/test_app.py
git commit -m "app: inject the GPU collector into the refresh loop

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Snapshot field and web collection

**Files:**
- Modify: `src/harbor_console/snapshot.py`
- Modify: `src/harbor_console/webapp.py`
- Modify: `tests/test_webapp.py`

**Interfaces:**
- Consumes: `GpuEntry`, `collect_gpus` from `harbor_console.gpu`.
- Produces: `Snapshot.gpus: tuple[GpuEntry, ...] = ()`; `collect_snapshot(..., gpus: Callable[[], tuple[GpuEntry, ...]] = collect_gpus)`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_webapp.py`, add `from harbor_console.gpu import GpuEntry` after the `StorageEntry` import and append:

```python
def test_collect_snapshot_populates_gpus_from_the_injected_collector():
    entry = GpuEntry(label="GPU card0 (radeon)", driver="radeon", temp_c=35.0)

    snapshot = collect(gpus=lambda: (entry,))

    assert snapshot.gpus == (entry,)


def test_collect_snapshot_defaults_to_the_real_gpu_collector():
    assert (
        inspect.signature(webapp.collect_snapshot).parameters["gpus"].default
        is webapp.collect_gpus
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_webapp.py -k gpu -v`
Expected: both FAIL (`unexpected keyword argument 'gpus'`, `KeyError: 'gpus'`).

- [ ] **Step 3: Add the field and the parameter**

In `src/harbor_console/snapshot.py`, add the import next to the storage one:

```python
from harbor_console.gpu import GpuEntry
from harbor_console.storage import StorageEntry
```

and after the `storage` field at the end of the dataclass:

```python
    #: One entry per DRM card. Empty is a host with no GPU; `probed` is what
    #: distinguishes that from a cycle that has not run.
    gpus: tuple[GpuEntry, ...] = ()
```

In `src/harbor_console/webapp.py`, add the import next to the storage one:

```python
from harbor_console.gpu import GpuEntry, collect_gpus
from harbor_console.storage import StorageEntry, collect_storage
```

add the parameter to `collect_snapshot` after `storage`:

```python
    storage: Callable[[], tuple[StorageEntry, ...]] = collect_storage,
    gpus: Callable[[], tuple[GpuEntry, ...]] = collect_gpus,
    tailnet_address: str | None = None,
    own_port: int | None = WEB_PORT,
```

and pass it into the `Snapshot(...)` constructor after `storage=storage(),`:

```python
        storage=storage(),
        gpus=gpus(),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_webapp.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/snapshot.py src/harbor_console/webapp.py tests/test_webapp.py
git commit -m "webapp: carry GPU entries on the snapshot

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Web page section

**Files:**
- Modify: `src/harbor_console/web.py`
- Modify: `tests/test_web.py`

**Interfaces:**
- Consumes: `Snapshot.gpus` (Task 5), `format_gpu`, `GpuEntry` from `harbor_console.gpu`.
- Produces: `_gpu_section(snapshot: Snapshot) -> str`, a `<div class="gpu-section">` as the third cell of `.resource-grid`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_web.py`, add `from harbor_console.gpu import GpuEntry` after the `StorageEntry` import. Extend the existing escape test so the GPU section is covered: inside `test_page_escapes_every_field_that_originates_outside_this_project`, add a `gpu = GpuEntry(label="<g>", note="<h>")` next to `disk`, pass `gpus=(gpu,)` into the `snapshot(...)` call alongside `storage=(disk,)`, and add `assert "<g>" not in page` and `assert "<h>" not in page` next to the existing `<w>` / `<x>` assertions. Then append:

```python
GPUS = (
    GpuEntry(label="GPU card0 (radeon)", driver="radeon", temp_c=35.0),
    GpuEntry(
        label="GPU card1 (amdgpu)",
        driver="amdgpu",
        busy_percent=12,
        vram_used=1024**3,
        vram_total=8 * 1024**3,
        temp_c=54.0,
    ),
)


def test_page_shows_every_gpu_entry():
    page = web.render_page(snapshot(gpus=GPUS)).decode()

    assert '<div class="gpu-section"><h2>GPU</h2>' in page
    assert "GPU card0 (radeon)" in page
    assert "35 °C" in page
    assert "GPU card1 (amdgpu)" in page
    assert "busy 12% · VRAM 1.0 / 8.0 GiB (12.5%) · 54 °C" in page


def test_gpu_section_sits_in_the_resource_grid_after_storage():
    page = web.render_page(snapshot(gpus=GPUS)).decode()

    assert page.index('class="storage-section"') < page.index('class="gpu-section"')
    assert page.index('class="gpu-section"') < page.index("<h2>Directory</h2>")


def test_gpu_before_the_first_cycle_says_so_rather_than_none_detected():
    page = web.render_page(snapshot(probed=False, gpus=())).decode()

    assert "<h2>GPU</h2><p>Nothing has been collected yet" in page
    assert "none detected" not in page


def test_gpu_probed_but_empty_says_none_detected():
    page = web.render_page(snapshot(probed=True, gpus=())).decode()

    assert "<h2>GPU</h2><p>No GPU detected.</p>" in page
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web.py -k "gpu or escapes" -v`
Expected: the four new tests FAIL (`gpu-section` absent); the escape test FAILS only if the `gpus=` keyword is rejected -- it is accepted after Task 5, so it passes until a `<g>` leaks, which it cannot before the section exists. Confirm the four `gpu` tests fail.

- [ ] **Step 3: Add the section**

In `src/harbor_console/web.py`, add the import next to the storage one:

```python
from harbor_console.gpu import format_gpu
from harbor_console.storage import format_entry
```

Change the resource grid to three cells:

```python
    parts.append(
        '<div class="resource-grid">'
        f'<div class="host-section">{_host_table(snapshot)}</div>'
        f'<div class="storage-section">{_storage_section(snapshot)}</div>'
        f'<div class="gpu-section">{_gpu_section(snapshot)}</div>'
        "</div>"
    )
```

Add the section renderer after `_storage_section`:

```python
def _gpu_section(snapshot: Snapshot) -> str:
    """One row per card, whatever its driver exposed.

    The three states storage has: not yet collected, collected and empty,
    rows. "No GPU detected" is a fact about the host, so it is said outright
    rather than left as a blank cell.
    """
    if not snapshot.probed:
        return (
            "<h2>GPU</h2><p>Nothing has been collected yet: the first cycle "
            "has not finished.</p>"
        )
    if not snapshot.gpus:
        return "<h2>GPU</h2><p>No GPU detected.</p>"
    rows = "".join(
        f"<tr><td>{escape(entry.label)}</td><td>{escape(format_gpu(entry))}</td></tr>"
        for entry in snapshot.gpus
    )
    return "<h2>GPU</h2><table>" + rows + "</table>"
```

Leave `.resource-grid` at two columns: the third cell wraps to a second row on wide screens and stacks on narrow ones, and the existing layout test pins the two-column rule.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/web.py tests/test_web.py
git commit -m "web: GPU section beside storage on the status page

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Documentation and full-suite check

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-09-25-gpu-monitoring-design.md` (status line only)

- [ ] **Step 1: Describe the collector in CLAUDE.md**

In the `## Architecture` list of `CLAUDE.md`, directly after the `storage.py` bullet, add:

```markdown
- `gpu.py` — **collects** what GPUs this host has, from `/sys/class/drm`: one entry per `card<N>`, with the driver name from `device/uevent` and, where the driver exposes them, busy percent, VRAM used/total and hwmon temperature. Every metric is optional and each file is guarded on its own, so the `radeon` driver's one number (temperature) and `amdgpu`'s five render through the same `format_gpu`. Reads files and runs no subprocess, so nothing can hang. An unlistable root is one `unavailable` entry; no cards is an empty tuple, which both renderers show as `none detected` rather than a blank. Shared by both surfaces like `storage.py`.
```

Also in the `harbor-console` bullet under `## What this is`, insert `one row per GPU,` after `memory and swap with their own scale,`.

- [ ] **Step 2: Mark the spec implemented**

Change the spec's `Status: approved, not yet implemented` line to `Status: implemented 2026-09-25`.

- [ ] **Step 3: Run the whole suite**

Run: `uv run pytest`
Expected: all pass, zero failures. Note the count in the commit message.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-09-25-gpu-monitoring-design.md
git commit -m "docs: describe gpu.py and mark the GPU spec implemented

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review

- **Spec coverage.** Collector shape, per-file guards, `uevent` driver, sort by card number, connector filtering, unavailable sentinel, empty tuple: Task 2. Formatter shapes: Task 1. `app.run` injection and third renderer argument: Task 4. `build_dashboard` rows and `none detected`: Task 3. `Snapshot.gpus`, `collect_snapshot(gpus=)`: Task 5. `_gpu_section`, three states, third grid cell: Task 6. Every test the spec lists has a matching test in Tasks 2, 3, 4, 5, 6. Out-of-scope items untouched. No ADR, per spec.
- **Placeholders.** None.
- **Type consistency.** `GpuEntry` field names (`label, driver, busy_percent, vram_used, vram_total, temp_c, note`) are identical in Tasks 1 through 6. `collect_gpus(drm_root)` matches between Task 2 and the default-check tests in Tasks 4 and 5 (`app.collect_gpus`, `webapp.collect_gpus` are the imported names). `format_gpu` output strings in Task 6's tests match Task 1's formatter (`busy 12% · VRAM 1.0 / 8.0 GiB (12.5%) · 54 °C`).
