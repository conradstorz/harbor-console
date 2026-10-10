# GPU Busy History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Both surfaces show, under each GPU row, the card's mean busy percent over the last 1 h, 3 h, 7 h, 24 h and 7 d, from a week of samples the web prober keeps on disk.

**Architecture:** The web prober samples each card's busy percent every 30 s cycle, persists the samples to `/var/lib/harbor-console/gpu-history.json` (a systemd `StateDirectory`), computes the five averages, and writes them into the verdict file the console already reads once per tick. The console stays a reader: zero writes, zero new reads. A new pure module `gpu_history.py` holds the policy (record, prune, average, format) and the file codec, in the shape of `verdict.py`.

**Tech Stack:** Python 3.13, stdlib only (`json`, `os`, `dataclasses`, `datetime`), `rich` for the console, `pytest` via `uv run pytest`.

Spec: `docs/superpowers/specs/2026-10-09-gpu-busy-history-design.md`. Read it first.

## Global Constraints

- Run every command with `uv run ...`. No pip, no venv activation. Do not chain commands with `&&`; one command per call.
- Collectors and codecs never raise on a hostile environment (`loads`, `read_history` return an empty history; `write_history` raises `OSError` and its one caller reports).
- The console never probes, never writes, and reads only the verdict file.
- Only busy percent is averaged. Windows are exactly `1h`, `3h`, `7h`, `24h`, `7d`. Retention is exactly 7 days. Coverage flag threshold is exactly 95% of the window.
- The row format is `1h 42% · 3h 38% · 7h 30% · 24h 25% · 7d 18% (2d)`, with `—` for a window with no samples. Separator is ` · ` (space, U+00B7, space), same as `format_gpu`.
- The console row label is `  busy avg` (two leading spaces). The fallback text on both surfaces is `not reported`.
- No colour, no sparkline, no configuration, no flag that moves the history path.
- Tests use plain values and fixed datetimes: no real time, sockets, Docker or NVML. Disk only through `tmp_path`.
- Every commit message ends with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Style of the existing code: module docstrings say what the module is and why; functions get a short docstring where the behaviour is not obvious from the name; `from __future__ import annotations` at the top of every module.

---

### Task 1: `GpuEntry.card`, the stable key for history

**Files:**
- Modify: `src/harbor_console/gpu.py` (`GpuEntry`, `_card_entry`, `collect_gpus`)
- Test: `tests/test_gpu.py`

**Interfaces:**
- Produces: `GpuEntry.card: str = ""` -- the DRM node name (`card0`, `card1`), empty on the `unavailable` sentinel. Every later task keys history by it.

- [ ] **Step 1: Write the failing tests**

Add to the end of `tests/test_gpu.py`:

```python
def test_collect_fills_card_with_the_node_name(tmp_path):
    card(tmp_path, "card0")
    card(tmp_path, "card2", driver="amdgpu")

    assert [e.card for e in collect_gpus(tmp_path)] == ["card0", "card2"]


def test_the_unavailable_sentinel_has_no_card(tmp_path):
    (entry,) = collect_gpus(tmp_path / "missing")

    assert entry.note == NOTE_UNAVAILABLE
    assert entry.card == ""
```

Then add `card="card0"` (or the matching node name) to every equality assertion against `collect_gpus` output in `tests/test_gpu.py`. They are at these lines (numbers as of this plan; search for `== GpuEntry(` and `== (GpuEntry(`):

- line 131: `GpuEntry(label="GPU card0 (radeon)", driver="radeon", temp_c=35.0)` -> add `card="card0"`
- line 178: the `amdgpu` full-metrics entry -> add `card="card0"`
- line 217: `GpuEntry(label="GPU card0 (radeon)", driver="radeon")` -> add `card="card0"`
- line 226: `GpuEntry(label="GPU card0")` -> `GpuEntry(label="GPU card0", card="card0")`
- line 287: `(GpuEntry(label="GPU card0"),)` -> `(GpuEntry(label="GPU card0", card="card0"),)`
- line 358: the NVML-filled `card1` entry -> add `card="card1"`
- line 401: `GpuEntry(label="GPU card1 (nvidia)", driver="nvidia")` -> add `card="card1"`

Entries built only for `format_gpu` (lines 7-105) need no change: the field defaults to `""`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_gpu.py -q`
Expected: FAIL -- `TypeError: GpuEntry.__init__() got an unexpected keyword argument 'card'` on the edited assertions and the two new tests.

- [ ] **Step 3: Add the field and fill it**

In `src/harbor_console/gpu.py`, change the dataclass:

```python
@dataclass(frozen=True)
class GpuEntry:
    """One GPU, with whatever its driver chose to say about it."""

    label: str
    #: The DRM node (`card1`): the one name for a card that survives NVML
    #: going quiet for a cycle, so it is what history is keyed by. Empty on
    #: the `unavailable` sentinel, which is not a card.
    card: str = ""
    driver: str = ""
```

(the remaining fields stay as they are). In `_card_entry`, add `card=node.name` to the `GpuEntry(...)` construction:

```python
    entry = GpuEntry(
        label=_label(node.name, driver),
        card=node.name,
        driver=driver,
        busy_percent=_read_int(device / "gpu_busy_percent"),
        vram_used=_read_int(device / "mem_info_vram_used"),
        vram_total=_read_int(device / "mem_info_vram_total"),
        temp_c=_temperature(sensor),
        fan_percent=_fan_percent(sensor),
    )
```

`collect_gpus`'s sentinel `GpuEntry(label="GPU", note=NOTE_UNAVAILABLE)` is unchanged: `card` defaults to `""`.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass. (`tests/test_ui.py`, `tests/test_web.py`, `tests/test_webapp.py` build `GpuEntry` by keyword and never compare against `collect_gpus`, so they are untouched.)

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/gpu.py tests/test_gpu.py
git commit -m "feat: name the DRM node on each GpuEntry as its history key

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `gpu_history.py` policy: record, prune, average

**Files:**
- Create: `src/harbor_console/gpu_history.py`
- Test: `tests/test_gpu_history.py` (new)

**Interfaces:**
- Consumes: `GpuEntry.card`, `GpuEntry.busy_percent` from Task 1.
- Produces:
  - `Sample(at: datetime, busy_percent: int)` frozen dataclass.
  - `History = dict[str, tuple[Sample, ...]]` (card -> samples, oldest first).
  - `RETENTION = timedelta(days=7)`.
  - `WINDOWS: tuple[tuple[str, int], ...] = (("1h", 3600), ("3h", 10800), ("7h", 25200), ("24h", 86400), ("7d", 604800))`.
  - `WindowAverage(window: str, seconds: int, mean: int | None, covered_seconds: int)` frozen dataclass.
  - `GpuAverages(card: str, windows: tuple[WindowAverage, ...])` frozen dataclass.
  - `record(history: History, entries: tuple[GpuEntry, ...], now: datetime) -> History`.
  - `averages(history: History, now: datetime) -> tuple[GpuAverages, ...]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gpu_history.py`:

```python
from datetime import datetime, timedelta

from harbor_console.gpu import GpuEntry
from harbor_console.gpu_history import (
    RETENTION,
    WINDOWS,
    GpuAverages,
    Sample,
    WindowAverage,
    averages,
    record,
)

NOW = datetime(2026, 10, 9, 12, 0, 0)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def entry(card: str, busy: int | None) -> GpuEntry:
    return GpuEntry(label=f"GPU {card}", card=card, driver="amdgpu", busy_percent=busy)


# --- record ---------------------------------------------------------------


def test_record_appends_one_sample_per_card_with_a_busy_percent():
    history = record({}, (entry("card0", 12), entry("card1", 99)), NOW)

    assert history == {
        "card0": (Sample(NOW, 12),),
        "card1": (Sample(NOW, 99),),
    }


def test_record_skips_a_card_whose_driver_said_nothing():
    history = record({}, (entry("card0", None),), NOW)

    assert history == {}


def test_record_skips_the_unavailable_sentinel():
    sentinel = GpuEntry(label="GPU", note="unavailable", busy_percent=5)

    assert record({}, (sentinel,), NOW) == {}


def test_record_appends_after_existing_samples_oldest_first():
    earlier = {"card0": (Sample(NOW - HOUR, 10),)}

    history = record(earlier, (entry("card0", 20),), NOW)

    assert history["card0"] == (Sample(NOW - HOUR, 10), Sample(NOW, 20))


def test_record_prunes_samples_older_than_the_retention():
    old = Sample(NOW - RETENTION - timedelta(seconds=1), 1)
    edge = Sample(NOW - RETENTION, 2)
    history = {"card0": (old, edge)}

    assert record(history, (entry("card0", 3),), NOW)["card0"] == (edge, Sample(NOW, 3))


def test_record_drops_a_card_with_nothing_left():
    history = {"card9": (Sample(NOW - RETENTION - DAY, 1),)}

    assert record(history, (), NOW) == {}


def test_record_never_mutates_its_input():
    original = {"card0": (Sample(NOW - HOUR, 10),)}
    snapshot = {card: samples for card, samples in original.items()}

    record(original, (entry("card0", 20),), NOW)

    assert original == snapshot


def test_record_keeps_a_card_that_went_quiet_this_cycle():
    history = {"card0": (Sample(NOW - HOUR, 10),)}

    assert record(history, (entry("card0", None),), NOW) == history


# --- averages -------------------------------------------------------------


def test_windows_are_the_five_the_surfaces_show():
    assert WINDOWS == (
        ("1h", 3600),
        ("3h", 3 * 3600),
        ("7h", 7 * 3600),
        ("24h", 86400),
        ("7d", 7 * 86400),
    )


def test_averages_returns_one_entry_per_card_in_card_order():
    history = {
        "card10": (Sample(NOW, 1),),
        "card2": (Sample(NOW, 2),),
        "card0": (Sample(NOW, 3),),
    }

    assert [a.card for a in averages(history, NOW)] == ["card0", "card2", "card10"]


def test_averages_has_every_window_for_a_card():
    (result,) = averages({"card0": (Sample(NOW, 50),)}, NOW)

    assert [(w.window, w.seconds) for w in result.windows] == list(WINDOWS)


def test_a_window_is_the_plain_rounded_mean_of_the_samples_inside_it():
    samples = (
        Sample(NOW - timedelta(minutes=50), 10),
        Sample(NOW - timedelta(minutes=30), 20),
        Sample(NOW - timedelta(minutes=10), 31),
    )

    (result,) = averages({"card0": samples}, NOW)
    one_hour = result.windows[0]

    assert one_hour == WindowAverage("1h", 3600, 20, 50 * 60)


def test_a_sample_on_the_window_boundary_is_inside_it():
    samples = (Sample(NOW - HOUR, 100), Sample(NOW, 0))

    (result,) = averages({"card0": samples}, NOW)

    assert result.windows[0].mean == 50
    assert result.windows[0].covered_seconds == 3600


def test_a_sample_just_outside_the_window_is_not_counted():
    samples = (Sample(NOW - HOUR - timedelta(seconds=1), 100), Sample(NOW, 0))

    (result,) = averages({"card0": samples}, NOW)

    assert result.windows[0] == WindowAverage("1h", 3600, 0, 0)
    assert result.windows[1].mean == 50  # the 3h window still sees both


def test_a_window_with_no_samples_has_no_mean_and_no_coverage():
    samples = (Sample(NOW - 2 * DAY, 40),)

    (result,) = averages({"card0": samples}, NOW)

    assert result.windows[3] == WindowAverage("24h", 86400, None, 0)
    assert result.windows[4] == WindowAverage("7d", 7 * 86400, 40, 2 * 86400)


def test_a_sample_after_now_is_outside_every_window():
    samples = (Sample(NOW + HOUR, 100), Sample(NOW, 10))

    (result,) = averages({"card0": samples}, NOW)

    assert all(w.mean == 10 for w in result.windows)


def test_coverage_is_now_minus_the_oldest_sample_inside_the_window():
    samples = (Sample(NOW - 3 * DAY, 10), Sample(NOW - 5 * HOUR, 30), Sample(NOW, 50))

    (result,) = averages({"card0": samples}, NOW)
    by_name = {w.window: w for w in result.windows}

    assert by_name["1h"].covered_seconds == 0
    assert by_name["7h"].covered_seconds == 5 * 3600
    assert by_name["24h"].covered_seconds == 5 * 3600
    assert by_name["7d"].covered_seconds == 3 * 86400


def test_averages_of_an_empty_history_is_empty():
    assert averages({}, NOW) == ()


def test_averages_is_a_tuple_of_gpu_averages():
    (result,) = averages({"card0": (Sample(NOW, 1),)}, NOW)

    assert isinstance(result, GpuAverages)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_gpu_history.py -q`
Expected: FAIL at import -- `ModuleNotFoundError: No module named 'harbor_console.gpu_history'`.

- [ ] **Step 3: Write the module**

Create `src/harbor_console/gpu_history.py`:

```python
"""A week of GPU busy samples, and the averages both surfaces show over it.

Policy and codec, in the shape of `verdict.py`. The web prober is the one
writer: each cycle it records one busy sample per card, prunes what is
older than a week, and hands the averages to the snapshot and the verdict
file. The console only ever sees the averages, inside the file it already
reads, so the 1 Hz loop gains no write and no new read (ADR 22).

Everything here is pure in `now`: the prober passes `snapshot.collected`,
and tests pass fixed datetimes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from harbor_console.gpu import GpuEntry

#: Samples older than this are dropped on every record.
RETENTION = timedelta(days=7)

#: The averages shown, in row order: name and length in seconds.
WINDOWS: tuple[tuple[str, int], ...] = (
    ("1h", 3600),
    ("3h", 3 * 3600),
    ("7h", 7 * 3600),
    ("24h", 86400),
    ("7d", 7 * 86400),
)

_CARD_NUMBER = re.compile(r"(\d+)$")


@dataclass(frozen=True)
class Sample:
    at: datetime
    busy_percent: int


#: Card name to its samples, oldest first.
History = dict[str, tuple[Sample, ...]]


@dataclass(frozen=True)
class WindowAverage:
    """One window's mean, and how much of the window the samples span.

    `mean` is `None` when no sample fell inside the window. `covered_seconds`
    is `now` minus the oldest sample inside the window, 0 when there is none:
    the honest cheap measure of how much of the window the mean speaks for.
    """

    window: str
    seconds: int
    mean: int | None
    covered_seconds: int


@dataclass(frozen=True)
class GpuAverages:
    card: str
    windows: tuple[WindowAverage, ...]


def record(history: History, entries: tuple[GpuEntry, ...], now: datetime) -> History:
    """`history` plus one sample at `now` per entry that reported a busy
    percent, minus everything older than `RETENTION`. A new mapping; the
    input is never touched.

    An entry with no `card` (the `unavailable` sentinel) or no busy percent
    (a driver that stayed silent this cycle) adds nothing: silence is never
    a zero. A card with no samples left is dropped.
    """
    cutoff = now - RETENTION
    updated: dict[str, tuple[Sample, ...]] = dict(history)
    for entry in entries:
        if not entry.card or entry.busy_percent is None:
            continue
        updated[entry.card] = updated.get(entry.card, ()) + (Sample(now, entry.busy_percent),)
    pruned = {
        card: tuple(sample for sample in samples if sample.at >= cutoff)
        for card, samples in updated.items()
    }
    return {card: samples for card, samples in pruned.items() if samples}


def averages(history: History, now: datetime) -> tuple[GpuAverages, ...]:
    """Every window for every card in `history`, cards in number order."""
    return tuple(
        GpuAverages(
            card,
            tuple(_window(history[card], name, seconds, now) for name, seconds in WINDOWS),
        )
        for card in sorted(history, key=_card_order)
    )


def _window(samples: tuple[Sample, ...], name: str, seconds: int, now: datetime) -> WindowAverage:
    start = now - timedelta(seconds=seconds)
    inside = [sample for sample in samples if start <= sample.at <= now]
    if not inside:
        return WindowAverage(name, seconds, None, 0)
    oldest = min(sample.at for sample in inside)
    mean = round(sum(sample.busy_percent for sample in inside) / len(inside))
    return WindowAverage(name, seconds, mean, int((now - oldest).total_seconds()))


def _card_order(card: str) -> tuple[int, str]:
    """`card2` before `card10`: numeric where there is a number, name otherwise."""
    match = _CARD_NUMBER.search(card)
    return (int(match.group(1)) if match else -1, card)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_gpu_history.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/gpu_history.py tests/test_gpu_history.py
git commit -m "feat: record a week of GPU busy samples and average them over five windows

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `format_averages`

**Files:**
- Modify: `src/harbor_console/gpu_history.py`
- Test: `tests/test_gpu_history.py`

**Interfaces:**
- Consumes: `WindowAverage`, `GpuAverages` from Task 2.
- Produces: `format_averages(entry: GpuAverages) -> str`; constants `FULL_COVERAGE = 0.95`, `NO_MEAN = "—"`. Both renderers call this and nothing else.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_gpu_history.py`:

```python
from harbor_console.gpu_history import FULL_COVERAGE, NO_MEAN, format_averages  # noqa: E402


def window(name: str, seconds: int, mean: int | None, covered: int | None = None) -> WindowAverage:
    """A window; coverage defaults to full."""
    return WindowAverage(name, seconds, mean, seconds if covered is None else covered)


def test_format_joins_every_window_in_order():
    entry = GpuAverages(
        "card1",
        (
            window("1h", 3600, 42),
            window("3h", 3 * 3600, 38),
            window("7h", 7 * 3600, 30),
            window("24h", 86400, 25),
            window("7d", 7 * 86400, 18),
        ),
    )

    assert format_averages(entry) == "1h 42% · 3h 38% · 7h 30% · 24h 25% · 7d 18%"


def test_format_flags_a_window_the_samples_do_not_cover():
    entry = GpuAverages("card1", (window("7d", 7 * 86400, 18, covered=2 * 86400),))

    assert format_averages(entry) == "7d 18% (2d)"


def test_format_does_not_flag_coverage_at_or_above_the_threshold():
    assert FULL_COVERAGE == 0.95
    just_enough = GpuAverages("card1", (window("1h", 3600, 5, covered=3420),))
    just_short = GpuAverages("card1", (window("1h", 3600, 5, covered=3419),))

    assert format_averages(just_enough) == "1h 5%"
    assert format_averages(just_short) == "1h 5% (56m)"


def test_format_shows_the_span_in_minutes_hours_or_days_floored():
    cases = [
        (window("1h", 3600, 1, covered=0), "1h 1% (0m)"),
        (window("1h", 3600, 1, covered=59 * 60 + 59), "1h 1% (59m)"),
        (window("24h", 86400, 1, covered=3600), "24h 1% (1h)"),
        (window("24h", 86400, 1, covered=23 * 3600 + 3599), "24h 1% (23h)"),
        (window("7d", 7 * 86400, 1, covered=86400), "7d 1% (1d)"),
        (window("7d", 7 * 86400, 1, covered=6 * 86400 + 86399), "7d 1% (6d)"),
    ]

    for w, expected in cases:
        assert format_averages(GpuAverages("card1", (w,))) == expected


def test_format_shows_a_dash_for_a_window_with_no_samples():
    entry = GpuAverages("card1", (window("1h", 3600, 5), WindowAverage("24h", 86400, None, 0)))

    assert NO_MEAN == "—"
    assert format_averages(entry) == "1h 5% · 24h —"


def test_format_fits_the_steady_partial_row_in_the_console_value_column():
    """From one day after a fresh history until the week is full, only the
    7 d window carries a flag. 80 columns minus the panel border, padding
    and the GPU label leaves 54 for the value (see `test_gpu.py`); this
    row has to fit beside the instantaneous one without wrapping."""
    entry = GpuAverages(
        "card1",
        (
            window("1h", 3600, 100),
            window("3h", 3 * 3600, 100),
            window("7h", 7 * 3600, 100),
            window("24h", 86400, 100),
            window("7d", 7 * 86400, 100, covered=6 * 86400),
        ),
    )

    assert format_averages(entry) == "1h 100% · 3h 100% · 7h 100% · 24h 100% · 7d 100% (6d)"
    assert len(format_averages(entry)) <= 54
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_gpu_history.py -q`
Expected: FAIL at import -- `ImportError: cannot import name 'FULL_COVERAGE'`.

- [ ] **Step 3: Add the formatter**

In `src/harbor_console/gpu_history.py`, add after `WINDOWS`:

```python
#: Below this share of a window, the span the samples cover is shown
#: beside the mean, so a fresh history never passes for a full one.
FULL_COVERAGE = 0.95

#: What a window with no samples shows in place of a percentage.
NO_MEAN = "—"
```

and at the end of the module:

```python
def format_averages(entry: GpuAverages) -> str:
    """`1h 42% · 3h 38% · 7h 30% · 24h 25% · 7d 18% (2d)`.

    A window the samples cover less than `FULL_COVERAGE` of carries the span
    they do cover, floored to the coarsest whole unit: minutes under an
    hour, hours under a day, days beyond. A window with no samples shows
    `NO_MEAN` and no span. Shared by both renderers, like `format_gpu`.
    """
    return " · ".join(_format_window(w) for w in entry.windows)


def _format_window(w: WindowAverage) -> str:
    if w.mean is None:
        return f"{w.window} {NO_MEAN}"
    text = f"{w.window} {w.mean}%"
    if w.covered_seconds < FULL_COVERAGE * w.seconds:
        text += f" ({_span(w.covered_seconds)})"
    return text


def _span(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_gpu_history.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/gpu_history.py tests/test_gpu_history.py
git commit -m "feat: render the five GPU busy averages on one row

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: the history file codec

**Files:**
- Modify: `src/harbor_console/gpu_history.py`
- Test: `tests/test_gpu_history.py`

**Interfaces:**
- Consumes: `Sample`, `History` from Task 2.
- Produces: `HISTORY_PATH = Path("/var/lib/harbor-console/gpu-history.json")`, `dumps(history: History) -> str`, `loads(text: str) -> History` (never raises), `read_history(path: Path = HISTORY_PATH) -> History` (never raises), `write_history(history: History, path: Path = HISTORY_PATH) -> None` (atomic; raises `OSError`).
- File shape: `{"cards": {"card1": [[epoch_seconds, busy_percent], ...]}}`, integer epoch seconds, oldest first. Timestamps are naive local datetimes in memory (the prober uses `datetime.now()`), converted with `datetime.timestamp()` / `datetime.fromtimestamp()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_gpu_history.py`:

```python
import json  # noqa: E402

import pytest  # noqa: E402

from harbor_console.gpu_history import (  # noqa: E402
    HISTORY_PATH,
    dumps,
    loads,
    read_history,
    write_history,
)

HISTORY = {
    "card0": (Sample(NOW - HOUR, 10), Sample(NOW, 20)),
    "card1": (Sample(NOW, 99),),
}


def test_history_lives_in_the_web_units_state_directory():
    assert str(HISTORY_PATH) == "/var/lib/harbor-console/gpu-history.json"


def test_codec_round_trip():
    assert loads(dumps(HISTORY)) == HISTORY


def test_dumps_is_compact_json_of_epoch_busy_pairs():
    payload = json.loads(dumps(HISTORY))

    assert payload == {
        "cards": {
            "card0": [[int((NOW - HOUR).timestamp()), 10], [int(NOW.timestamp()), 20]],
            "card1": [[int(NOW.timestamp()), 99]],
        }
    }
    assert "\n" not in dumps(HISTORY)


def test_dumps_drops_sub_second_precision_on_the_way_out():
    history = {"card0": (Sample(NOW.replace(microsecond=500000), 1),)}

    assert loads(dumps(history)) == {"card0": (Sample(NOW, 1),)}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        "{}",
        '{"cards": []}',
        '{"cards": {"card0": "x"}}',
        '{"cards": {"card0": [[1, 2, 3]]}}',
        '{"cards": {"card0": [["1", 2]]}}',
        '{"cards": {"card0": [[1.5, 2]]}}',
        '{"cards": {"card0": [[1, true]]}}',
        '{"cards": {"card0": [[1, 2]], "card1": [[1]]}}',
        '{"cards": {"card0": [[1e20, 2]]}}',
        '{"cards": {7: [[1, 2]]}}',
    ],
)
def test_loads_returns_an_empty_history_for_anything_malformed(text):
    assert loads(text) == {}


def test_loads_keeps_samples_in_file_order():
    text = '{"cards": {"card0": [[1000, 1], [500, 2]]}}'

    assert [s.busy_percent for s in loads(text)["card0"]] == [1, 2]


def test_loads_drops_a_card_with_no_samples():
    assert loads('{"cards": {"card0": []}}') == {}


def test_write_then_read(tmp_path):
    path = tmp_path / "gpu-history.json"

    write_history(HISTORY, path)

    assert read_history(path) == HISTORY


def test_write_leaves_no_temp_file_behind(tmp_path):
    path = tmp_path / "gpu-history.json"

    write_history(HISTORY, path)

    assert [p.name for p in tmp_path.iterdir()] == ["gpu-history.json"]


def test_write_raises_when_the_directory_is_missing(tmp_path):
    with pytest.raises(OSError):
        write_history(HISTORY, tmp_path / "missing" / "gpu-history.json")


def test_read_is_empty_for_a_missing_file(tmp_path):
    assert read_history(tmp_path / "missing.json") == {}


def test_read_is_empty_for_garbage(tmp_path):
    path = tmp_path / "gpu-history.json"
    path.write_text("{{{", encoding="utf-8")

    assert read_history(path) == {}


def test_read_defaults_to_the_state_directory_path():
    import inspect

    assert inspect.signature(read_history).parameters["path"].default is HISTORY_PATH
    assert inspect.signature(write_history).parameters["path"].default is HISTORY_PATH
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_gpu_history.py -q`
Expected: FAIL at import -- `ImportError: cannot import name 'HISTORY_PATH'`.

- [ ] **Step 3: Add the codec**

In `src/harbor_console/gpu_history.py`, add to the imports:

```python
import json
import os
from pathlib import Path
```

add after `NO_MEAN`:

```python
#: Under the web unit's `StateDirectory`: the one place this project keeps
#: anything across a restart or a reboot (ADR 22). No flag moves it.
HISTORY_PATH = Path("/var/lib/harbor-console/gpu-history.json")
```

and at the end of the module:

```python
def dumps(history: History) -> str:
    """`{"cards": {"card1": [[epoch_seconds, busy_percent], ...]}}`, on one
    line: at ~20k pairs per card a week, the file is rewritten every cycle
    and indentation would double it for nothing."""
    return json.dumps(
        {
            "cards": {
                card: [[int(sample.at.timestamp()), sample.busy_percent] for sample in samples]
                for card, samples in history.items()
            }
        },
        separators=(",", ":"),
    )


def loads(text: str) -> History:
    """Parse a history, or an empty one for anything that is not a history.
    Never raises: a malformed file costs the week it held, and the next
    cycle starts a new one."""
    try:
        payload = json.loads(text)
        cards = payload["cards"]
        if not isinstance(payload, dict) or not isinstance(cards, dict):
            return {}
        history: History = {}
        for card, pairs in cards.items():
            if not isinstance(card, str) or not isinstance(pairs, list):
                return {}
            samples = []
            for pair in pairs:
                if not (isinstance(pair, list) and len(pair) == 2):
                    return {}
                at, busy = pair
                if not (_is_int(at) and _is_int(busy)):
                    return {}
                samples.append(Sample(datetime.fromtimestamp(at), busy))
            if samples:
                history[card] = tuple(samples)
        return history
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return {}


def _is_int(value: object) -> bool:
    """`True` for an int that is not a bool; JSON `true` parses as a bool
    and a bool is an int to `isinstance`."""
    return isinstance(value, int) and not isinstance(value, bool)


def read_history(path: Path = HISTORY_PATH) -> History:
    """The history on disk, or an empty one. Never raises."""
    try:
        return loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_history(history: History, path: Path = HISTORY_PATH) -> None:
    """Replace the file atomically. Raises OSError; the caller reports it."""
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(dumps(history), encoding="utf-8")
    os.replace(temp, path)
```

Note on `loads`: `payload["cards"]` on a list raises `TypeError`, on a string `TypeError`, on a dict without the key `KeyError`; all land in the `except`. `datetime.fromtimestamp(1e20)` is unreachable (`1e20` parses as a float and fails `_is_int`), but a huge int raises `OverflowError`/`OSError`/`ValueError` depending on platform, which is why all three are caught.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_gpu_history.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/gpu_history.py tests/test_gpu_history.py
git commit -m "feat: persist the GPU busy history as one compact JSON file

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: the verdict carries the averages

**Files:**
- Modify: `src/harbor_console/verdict.py`
- Test: `tests/test_verdict.py`

**Interfaces:**
- Consumes: `GpuAverages`, `WindowAverage` from Task 2.
- Produces: `Verdict.gpus: tuple[GpuAverages, ...] = ()` as the last field; JSON key `"gpus"`, a list of `{"card": str, "windows": [{"window": str, "seconds": int, "mean": int | null, "covered_seconds": int}, ...]}`. A missing `"gpus"` key loads as `()`; a malformed one makes `loads` return `None`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_verdict.py`, add to the imports:

```python
from harbor_console.gpu_history import GpuAverages, WindowAverage
```

add after `VERDICT`:

```python
AVERAGES = (
    GpuAverages(
        "card1",
        (
            WindowAverage("1h", 3600, 42, 3600),
            WindowAverage("7d", 7 * 86400, None, 0),
        ),
    ),
)
WITH_GPUS = Verdict(
    written=VERDICT.written,
    hostname=VERDICT.hostname,
    checks=VERDICT.checks,
    gpus=AVERAGES,
)
```

and append these tests:

```python
def test_round_trip_with_gpu_averages():
    assert loads(dumps(WITH_GPUS)) == WITH_GPUS


def test_dumps_writes_the_averages_as_plain_json():
    payload = json.loads(dumps(WITH_GPUS))

    assert payload["gpus"] == [
        {
            "card": "card1",
            "windows": [
                {"window": "1h", "seconds": 3600, "mean": 42, "covered_seconds": 3600},
                {"window": "7d", "seconds": 604800, "mean": None, "covered_seconds": 0},
            ],
        }
    ]


def test_a_verdict_without_gpus_has_none():
    assert VERDICT.gpus == ()
    assert json.loads(dumps(VERDICT))["gpus"] == []


def test_loads_treats_a_missing_gpus_key_as_empty():
    """An older writer and a newer reader coexist across a deploy."""
    payload = json.loads(dumps(VERDICT))
    del payload["gpus"]

    assert loads(json.dumps(payload)) == VERDICT


@pytest.mark.parametrize(
    "gpus",
    [
        "none",
        [1],
        [{"card": "card1"}],
        [{"card": 1, "windows": []}],
        [{"card": "card1", "windows": "x"}],
        [{"card": "card1", "windows": [{"window": "1h"}]}],
        [{"card": "card1", "windows": [{"window": "1h", "seconds": "3600", "mean": 1, "covered_seconds": 0}]}],
        [{"card": "card1", "windows": [{"window": "1h", "seconds": 3600, "mean": "1", "covered_seconds": 0}]}],
        [{"card": "card1", "windows": [{"window": "1h", "seconds": 3600, "mean": 1, "covered_seconds": None}]}],
        [{"card": "card1", "windows": [{"window": 1, "seconds": 3600, "mean": 1, "covered_seconds": 0}]}],
    ],
)
def test_loads_returns_none_for_malformed_gpus(gpus):
    payload = json.loads(dumps(VERDICT))
    payload["gpus"] = gpus

    assert loads(json.dumps(payload)) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_verdict.py -q`
Expected: FAIL -- `TypeError: Verdict.__init__() got an unexpected keyword argument 'gpus'` at collection of `WITH_GPUS`.

- [ ] **Step 3: Extend the contract**

In `src/harbor_console/verdict.py`:

Add the import:

```python
from harbor_console.gpu_history import GpuAverages, WindowAverage
```

Extend the dataclass:

```python
@dataclass(frozen=True)
class Verdict:
    """One cycle's checks, stamped with when the prober wrote them, and the
    GPU busy averages the prober keeps (ADR 22): the console shows them
    under each card and computes nothing."""

    written: datetime
    hostname: str
    checks: tuple[Check, ...]
    gpus: tuple[GpuAverages, ...] = ()
```

Replace `dumps`:

```python
def dumps(verdict: Verdict) -> str:
    return json.dumps(
        {
            "written": verdict.written.isoformat(),
            "hostname": verdict.hostname,
            "checks": [
                {"name": c.name, "state": c.state, "reason": c.reason} for c in verdict.checks
            ],
            "gpus": [
                {
                    "card": g.card,
                    "windows": [
                        {
                            "window": w.window,
                            "seconds": w.seconds,
                            "mean": w.mean,
                            "covered_seconds": w.covered_seconds,
                        }
                        for w in g.windows
                    ],
                }
                for g in verdict.gpus
            ],
        },
        indent=2,
    )
```

In `loads`, inside the existing `try`, after `checks = tuple(checks)` add:

```python
        gpus = _load_gpus(payload.get("gpus", []))
```

change the final return to:

```python
    return Verdict(written=written, hostname=hostname, checks=checks, gpus=gpus)
```

and add after `loads`:

```python
def _load_gpus(raw: object) -> tuple[GpuAverages, ...]:
    """The `gpus` list, or a `ValueError` for anything that is not one; a
    missing key is passed in as `[]` by the caller, so an older writer
    still loads."""
    if not isinstance(raw, list):
        raise ValueError("gpus is not a list")
    entries = []
    for g in raw:
        if not isinstance(g, dict) or not isinstance(g["card"], str) or not isinstance(g["windows"], list):
            raise ValueError("malformed gpu entry")
        windows = []
        for w in g["windows"]:
            if not isinstance(w, dict):
                raise ValueError("malformed window")
            window, seconds, mean, covered = w["window"], w["seconds"], w["mean"], w["covered_seconds"]
            if not (isinstance(window, str) and _is_int(seconds) and _is_int(covered)):
                raise ValueError("malformed window")
            if mean is not None and not _is_int(mean):
                raise ValueError("malformed window")
            windows.append(WindowAverage(window, seconds, mean, covered))
        entries.append(GpuAverages(g["card"], tuple(windows)))
    return tuple(entries)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
```

The `except (KeyError, TypeError, ValueError)` already around the body of `loads` turns every one of those into `None`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_verdict.py tests/test_check.py tests/test_ui.py -q`
Expected: all pass (`check.py` and `ui.py` read verdicts and must still accept the new field by default).

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/verdict.py tests/test_verdict.py
git commit -m "feat: carry the GPU busy averages in the verdict file

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: the web prober keeps the history and publishes the averages

**Files:**
- Modify: `src/harbor_console/snapshot.py` (one field)
- Modify: `src/harbor_console/webapp.py` (`GpuHistoryKeeper`, `collect_snapshot`, `VerdictPublisher`, `_default_prober`)
- Test: `tests/test_webapp.py`

**Interfaces:**
- Consumes: `record`, `averages`, `read_history`, `write_history`, `HISTORY_PATH`, `GpuAverages` from Tasks 2 and 4; `Verdict.gpus` from Task 5.
- Produces:
  - `Snapshot.gpu_averages: tuple[GpuAverages, ...] = ()`.
  - `webapp.HistoryKeeper = Callable[[tuple[GpuEntry, ...], datetime], tuple[GpuAverages, ...]]` type alias and `webapp.no_history(entries, now) -> ()`.
  - `webapp.GpuHistoryKeeper(path=HISTORY_PATH, reader=read_history, writer=write_history, report=_report)`, callable as a `HistoryKeeper`.
  - `collect_snapshot(..., history: HistoryKeeper = no_history)`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_webapp.py`, add to the imports:

```python
from harbor_console.gpu_history import GpuAverages, Sample, WindowAverage, read_history, write_history
```

and append these tests:

```python
# --- GPU history ----------------------------------------------------------

BUSY = GpuEntry(label="GPU card1 (RTX 3060)", card="card1", driver="nvidia", busy_percent=40)


def test_collect_snapshot_hands_the_gpus_and_now_to_the_history_keeper():
    seen = {}

    def keeper(entries, now):
        seen["entries"], seen["now"] = entries, now
        return (GpuAverages("card1", ()),)

    snapshot = collect(gpus=lambda: (BUSY,), history=keeper)

    assert seen == {"entries": (BUSY,), "now": NOW}
    assert snapshot.gpu_averages == (GpuAverages("card1", ()),)


def test_collect_snapshot_defaults_to_no_history():
    assert inspect.signature(webapp.collect_snapshot).parameters["history"].default is webapp.no_history
    assert webapp.no_history((BUSY,), NOW) == ()
    assert collect(gpus=lambda: (BUSY,)).gpu_averages == ()


def test_the_starting_snapshot_has_no_averages():
    assert webapp.starting_snapshot("hpz440", NOW).gpu_averages == ()


def test_history_keeper_records_writes_and_returns_the_averages(tmp_path):
    path = tmp_path / "gpu-history.json"
    keeper = webapp.GpuHistoryKeeper(path)

    result = keeper((BUSY,), NOW)

    assert read_history(path) == {"card1": (Sample(NOW, 40),)}
    assert [a.card for a in result] == ["card1"]
    assert result[0].windows[0] == WindowAverage("1h", 3600, 40, 0)


def test_history_keeper_loads_what_an_earlier_life_wrote(tmp_path):
    path = tmp_path / "gpu-history.json"
    earlier = NOW - timedelta(minutes=30)
    write_history({"card1": (Sample(earlier, 20),)}, path)

    result = webapp.GpuHistoryKeeper(path)((BUSY,), NOW)

    assert result[0].windows[0] == WindowAverage("1h", 3600, 30, 30 * 60)
    assert read_history(path) == {"card1": (Sample(earlier, 20), Sample(NOW, 40))}


def test_history_keeper_starts_empty_when_the_file_is_missing_or_garbage(tmp_path):
    path = tmp_path / "gpu-history.json"
    path.write_text("{{{", encoding="utf-8")

    result = webapp.GpuHistoryKeeper(path)((BUSY,), NOW)

    assert result[0].windows[0].mean == 40
    assert read_history(path) == {"card1": (Sample(NOW, 40),)}


def test_history_keeper_reports_a_write_failure_once_per_distinct_error(tmp_path):
    reported = []
    keeper = webapp.GpuHistoryKeeper(tmp_path / "missing" / "gpu-history.json", report=reported.append)

    keeper((BUSY,), NOW)
    keeper((BUSY,), NOW + timedelta(seconds=30))

    assert len(reported) == 1
    assert "gpu-history.json" in reported[0]


def test_history_keeper_reports_again_after_a_different_error(tmp_path):
    reported = []
    errors = iter([OSError("first"), OSError("second")])

    def writer(_history, _path):
        raise next(errors)

    keeper = webapp.GpuHistoryKeeper(tmp_path / "gpu-history.json", writer=writer, report=reported.append)

    keeper((BUSY,), NOW)
    keeper((BUSY,), NOW)

    assert len(reported) == 2


def test_history_keeper_keeps_averaging_in_memory_while_the_disk_fails(tmp_path):
    def writer(_history, _path):
        raise OSError("read-only file system")

    keeper = webapp.GpuHistoryKeeper(tmp_path / "gpu-history.json", writer=writer, report=lambda _m: None)

    keeper((BUSY,), NOW)
    result = keeper((GpuEntry(label="g", card="card1", busy_percent=60),), NOW + timedelta(seconds=30))

    assert result[0].windows[0].mean == 50


def test_history_keeper_reports_nothing_once_the_disk_recovers(tmp_path):
    reported = []
    errors = iter([OSError("first")])

    def writer(history, path):
        try:
            raise next(errors)
        except StopIteration:
            write_history(history, path)

    keeper = webapp.GpuHistoryKeeper(tmp_path / "gpu-history.json", writer=writer, report=reported.append)

    keeper((BUSY,), NOW)
    keeper((BUSY,), NOW)
    keeper((BUSY,), NOW)

    assert len(reported) == 1


def test_history_keeper_defaults_to_the_state_directory_path():
    from harbor_console.gpu_history import HISTORY_PATH

    assert inspect.signature(webapp.GpuHistoryKeeper.__init__).parameters["path"].default is HISTORY_PATH


def test_verdict_publisher_carries_the_averages(tmp_path):
    path = tmp_path / "checks.json"
    averages = (GpuAverages("card1", (WindowAverage("1h", 3600, 40, 0),)),)
    snapshot = Snapshot(collected=datetime(2026, 9, 2, 1, 2, 3), metrics=METRICS, gpu_averages=averages)

    webapp.VerdictPublisher(path)(snapshot)

    assert read_verdict(path).gpus == averages
```

`tests/test_webapp.py` imports only `datetime` from `datetime`; change line 2 to `from datetime import datetime, timedelta`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_webapp.py -q`
Expected: FAIL -- the first new test fails with `TypeError: collect_snapshot() got an unexpected keyword argument 'history'`, the keeper tests with `AttributeError: module 'harbor_console.webapp' has no attribute 'GpuHistoryKeeper'`, the publisher test with `TypeError: Snapshot.__init__() got an unexpected keyword argument 'gpu_averages'`.

- [ ] **Step 3: Add the snapshot field**

In `src/harbor_console/snapshot.py`, add the import:

```python
from harbor_console.gpu_history import GpuAverages
```

and after the `gpus` field:

```python
    #: The busy averages the prober keeps per card (ADR 22). Empty until
    #: the first cycle, and empty for a card whose driver never reports a
    #: busy percent.
    gpu_averages: tuple[GpuAverages, ...] = ()
```

- [ ] **Step 4: Add the keeper and wire it**

In `src/harbor_console/webapp.py`:

Add the import:

```python
from harbor_console.gpu_history import (
    HISTORY_PATH,
    GpuAverages,
    History,
    averages,
    read_history,
    record,
    write_history,
)
```

After the `TRAEFIK_DASHBOARD_PASSWORD_PATH` constant, add:

```python
#: How the prober turns this cycle's GPU entries into averages: the keeper
#: in production, a fake in tests, nothing before either is wired.
HistoryKeeper = Callable[[tuple[GpuEntry, ...], datetime], tuple[GpuAverages, ...]]


def no_history(entries: tuple[GpuEntry, ...], now: datetime) -> tuple[GpuAverages, ...]:
    """The default: no history kept, no averages shown."""
    return ()
```

Add the parameter to `collect_snapshot`, after `own_port`:

```python
    history: HistoryKeeper = no_history,
```

and in its body replace `gpus=gpus(),` with:

```python
        gpus=cards,
        gpu_averages=history(cards, now),
```

where `cards = gpus()` is assigned next to `metrics = dict(collector())` at the top of the function:

```python
    metrics = dict(collector())
    cards = gpus()
```

In `VerdictPublisher.__call__`, add `gpus=snapshot.gpu_averages` to the `Verdict(...)` construction:

```python
        verdict = Verdict(
            written=snapshot.collected,
            hostname=str(snapshot.metrics.get("hostname", "")),
            checks=snapshot.checks,
            gpus=snapshot.gpu_averages,
        )
```

After `VerdictPublisher`, add:

```python
class GpuHistoryKeeper:
    """The one writer of the GPU busy history (ADR 22).

    Loads the file once, then each cycle records this cycle's entries,
    rewrites the file and returns the averages. A write that fails is
    reported once per distinct error, like `VerdictPublisher`, and the
    history still advances in memory: the page keeps its averages until the
    disk recovers, and loses at most what was recorded while it was down.
    Called only from the prober thread, so it needs no lock.
    """

    def __init__(
        self,
        path: Path = HISTORY_PATH,
        reader: Callable[[Path], History] = read_history,
        writer: Callable[[History, Path], None] = write_history,
        report: Callable[[str], None] = _report,
    ) -> None:
        self._path = path
        self._writer = writer
        self._report = report
        self._last_error: str | None = None
        self._history = reader(path)

    def __call__(self, entries: tuple[GpuEntry, ...], now: datetime) -> tuple[GpuAverages, ...]:
        self._history = record(self._history, entries, now)
        try:
            self._writer(self._history, self._path)
        except Exception as exc:  # noqa: BLE001 - every failure to write must reach the journal
            message = f"could not write {self._path}: {exc}"
            if message != self._last_error:
                self._report(message)
                self._last_error = message
        else:
            self._last_error = None
        return averages(self._history, now)
```

In `_default_prober`, construct one keeper and pass it:

```python
def _default_prober(holder: SnapshotHolder, tailnet_address: str) -> None:
    keeper = GpuHistoryKeeper()

    def collect() -> Snapshot:
        return collect_snapshot(
            datetime.now(),
            tailnet_address=tailnet_address,
            routers=lambda: traefik_routers(credentials=read_traefik_credentials()),
            history=keeper,
        )
```

(the rest of `_default_prober` is unchanged.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_webapp.py tests/test_web.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_console/snapshot.py src/harbor_console/webapp.py tests/test_webapp.py
git commit -m "feat: keep the GPU busy history in the web prober and publish its averages

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: the console shows the averages row

**Files:**
- Modify: `src/harbor_console/ui.py` (`build_dashboard`)
- Modify: `src/harbor_console/app.py` (`DashboardBuilder`, `run.frame`)
- Test: `tests/test_ui.py`, `tests/test_app.py`

**Interfaces:**
- Consumes: `GpuAverages`, `format_averages` from Tasks 2-3; `Verdict.gpus` from Task 5; `GpuEntry.card` from Task 1.
- Produces: `build_dashboard(metrics, storage=(), gpus=(), banner=None, gpu_averages=())`; the renderer callable `app.run` takes now receives five positional arguments `(metrics, storage, gpus, banner, gpu_averages)`.
- Constants in `ui.py`: `AVERAGES_LABEL = "  busy avg"`, `NOT_REPORTED = "not reported"`.

- [ ] **Step 1: Write the failing UI tests**

In `tests/test_ui.py`, add to the imports:

```python
from harbor_console.gpu_history import GpuAverages, WindowAverage
```

change the `render` helper to pass averages through:

```python
def render(metrics, storage=(), gpus=(), gpu_averages=()):
    """The panel as text. Wide enough that no cell wraps."""
    console = Console(width=120, record=True)
    console.print(build_dashboard(metrics, storage, gpus, None, gpu_averages))
    return console.export_text()
```

and append:

```python
AVERAGES = (
    GpuAverages(
        "card1",
        (
            WindowAverage("1h", 3600, 42, 3600),
            WindowAverage("3h", 3 * 3600, 38, 3 * 3600),
            WindowAverage("7h", 7 * 3600, 30, 7 * 3600),
            WindowAverage("24h", 86400, 25, 86400),
            WindowAverage("7d", 7 * 86400, 18, 2 * 86400),
        ),
    ),
)


def test_dashboard_shows_the_busy_averages_under_the_matching_card():
    gpus = (GpuEntry(label="GPU card1 (RTX 3060)", card="card1", driver="nvidia", busy_percent=7),)

    lines = render(METRICS, (), gpus, AVERAGES).splitlines()
    gpu_row = next(i for i, line in enumerate(lines) if "GPU card1 (RTX 3060)" in line)

    assert "busy avg" in lines[gpu_row + 1]
    assert "1h 42% · 3h 38% · 7h 30% · 24h 25% · 7d 18% (2d)" in lines[gpu_row + 1]


def test_dashboard_says_not_reported_for_a_card_without_averages():
    gpus = (GpuEntry(label="GPU card0 (radeon)", card="card0", driver="radeon", temp_c=35.0),)

    lines = render(METRICS, (), gpus, AVERAGES).splitlines()
    gpu_row = next(i for i, line in enumerate(lines) if "GPU card0 (radeon)" in line)

    assert "busy avg" in lines[gpu_row + 1]
    assert "not reported" in lines[gpu_row + 1]


def test_dashboard_says_not_reported_when_there_is_no_verdict_at_all():
    gpus = (GpuEntry(label="GPU card1 (RTX 3060)", card="card1", driver="nvidia", busy_percent=7),)

    page = render(METRICS, (), gpus)

    assert "busy avg" in page
    assert "not reported" in page


def test_dashboard_gives_the_unavailable_sentinel_no_averages_row():
    gpus = (GpuEntry(label="GPU", note="unavailable"),)

    page = render(METRICS, (), gpus, AVERAGES)

    assert "unavailable" in page
    assert "busy avg" not in page


def test_dashboard_gives_no_averages_row_when_there_are_no_gpus():
    page = render(METRICS, (), (), AVERAGES)

    assert "none detected" in page
    assert "busy avg" not in page


def test_dashboard_keeps_the_steady_averages_row_on_one_line_at_80_columns():
    gpus = (GpuEntry(label="GPU card1 (RTX 3060)", card="card1", driver="nvidia", busy_percent=100),)
    full = (
        GpuAverages(
            "card1",
            (
                WindowAverage("1h", 3600, 100, 3600),
                WindowAverage("3h", 3 * 3600, 100, 3 * 3600),
                WindowAverage("7h", 7 * 3600, 100, 7 * 3600),
                WindowAverage("24h", 86400, 100, 86400),
                WindowAverage("7d", 7 * 86400, 100, 6 * 86400),
            ),
        ),
    )

    console = Console(width=80, record=True)
    console.print(build_dashboard(METRICS, (), gpus, None, full))
    lines = console.export_text().splitlines()

    assert any("busy avg" in line and "7d 100% (6d)" in line for line in lines)


def test_dashboard_without_averages_renders_as_before():
    gpus = (GpuEntry(label="GPU card0 (radeon)", card="card0", driver="radeon", temp_c=35.0),)
    console = Console(width=120, record=True)

    console.print(build_dashboard(METRICS, (), gpus))
    page = console.export_text()

    assert "GPU card0 (radeon)" in page
    assert "busy avg" in page
```

- [ ] **Step 2: Run the UI tests to verify they fail**

Run: `uv run pytest tests/test_ui.py -q`
Expected: FAIL -- `TypeError: build_dashboard() takes from 1 to 4 positional arguments but 5 were given`.

- [ ] **Step 3: Render the row**

In `src/harbor_console/ui.py`, add the import:

```python
from harbor_console.gpu_history import GpuAverages, format_averages
```

add after `BANNER_STYLE`:

```python
#: Indented so it reads as belonging to the GPU row above it.
AVERAGES_LABEL = "  busy avg"
#: No verdict, or a verdict without this card: the prober has not said.
NOT_REPORTED = "not reported"
```

and change `build_dashboard`:

```python
def build_dashboard(
    metrics: dict[str, str | float | int],
    storage: tuple[StorageEntry, ...] = (),
    gpus: tuple[GpuEntry, ...] = (),
    banner: Text | None = None,
    gpu_averages: tuple[GpuAverages, ...] = (),
) -> Panel | Group:
    """Build a renderable dashboard panel from collected metrics, storage,
    GPUs and the busy averages the status page reported for them."""
```

and the GPU block:

```python
    # An empty tuple is a host with no card, and that is a fact worth a row:
    # a blank where the GPU line should be reads as a render bug.
    if gpus:
        by_card = {a.card: a for a in gpu_averages}
        for gpu in gpus:
            table.add_row(gpu.label, format_gpu(gpu))
            if gpu.card:
                entry = by_card.get(gpu.card)
                table.add_row(AVERAGES_LABEL, NOT_REPORTED if entry is None else format_averages(entry))
    else:
        table.add_row("GPU", "none detected")
```

- [ ] **Step 4: Run the UI tests to verify they pass**

Run: `uv run pytest tests/test_ui.py -q`
Expected: all pass.

- [ ] **Step 5: Write the failing app tests**

In `tests/test_app.py`, every fake renderer takes four positional arguments and `app.run` is about to pass five. Change each of the five fakes to accept the fifth:

- line 26: `def renderer(metrics, _storage, _gpus, _banner, _averages):`
- line 50: `def renderer(metrics, storage, _gpus, _banner, _averages):`
- line 74: `def renderer(metrics, _storage, gpus, _banner, _averages):`
- line 112: `def renderer(_metrics, _storage, _gpus, banner, _averages):`
- line 137: `def renderer(_metrics, _storage, _gpus, banner, _averages):`

Then append:

```python
def test_run_passes_the_verdicts_averages_to_the_renderer(monkeypatch):
    from harbor_console.gpu_history import GpuAverages

    seen = {}
    now = datetime(2026, 10, 9, 17, 21, 46)
    averages = (GpuAverages("card1", ()),)
    verdict = Verdict(now, "h", (), gpus=averages)

    def renderer(_metrics, _storage, _gpus, _banner, gpu_averages):
        seen["averages"] = gpu_averages
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
        verdict_reader=lambda: verdict,
        clock=lambda: now,
    )

    assert seen["averages"] == averages


def test_run_passes_no_averages_without_a_verdict(monkeypatch):
    seen = {}

    def renderer(_metrics, _storage, _gpus, _banner, gpu_averages):
        seen["averages"] = gpu_averages
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

    assert seen["averages"] == ()
```

- [ ] **Step 6: Run the app tests to verify they fail**

Run: `uv run pytest tests/test_app.py -q`
Expected: FAIL -- `TypeError: renderer() missing 1 required positional argument: '_averages'` on every test (the fakes now want five, `run` still passes four).

- [ ] **Step 7: Pass the averages through the loop**

In `src/harbor_console/app.py`, add the import:

```python
from harbor_console.gpu_history import GpuAverages
```

change the type alias:

```python
DashboardBuilder = Callable[
    [
        dict[str, str | float | int],
        tuple[StorageEntry, ...],
        tuple[GpuEntry, ...],
        object,
        tuple[GpuAverages, ...],
    ],
    object,
]
```

and `frame`:

```python
    def frame() -> object:
        now = clock()
        verdict = verdict_reader()
        banner = build_banner(verdict, now, started)
        averages = verdict.gpus if verdict is not None else ()
        return renderer(collector(), storage_collector(), gpu_collector(), banner, averages)
```

Update the docstring of `run` to add: "The same read carries the GPU busy averages the prober keeps; the console computes none of its own (ADR 22)."

- [ ] **Step 8: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add src/harbor_console/ui.py src/harbor_console/app.py tests/test_ui.py tests/test_app.py
git commit -m "feat: show each GPU's busy averages under its row on the console

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: the status page shows the averages row

**Files:**
- Modify: `src/harbor_console/web.py` (`_gpu_section`)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `Snapshot.gpu_averages` from Task 6; `format_averages` from Task 3; `ui.AVERAGES_LABEL`, `ui.NOT_REPORTED` from Task 7 (import them from `ui` so both surfaces agree on the wording by construction).

- [ ] **Step 1: Write the failing tests**

In `tests/test_web.py`, add to the imports:

```python
from harbor_console.gpu_history import GpuAverages, WindowAverage
```

and append:

```python
AVERAGES = (
    GpuAverages(
        "card1",
        (
            WindowAverage("1h", 3600, 42, 3600),
            WindowAverage("3h", 3 * 3600, 38, 3 * 3600),
            WindowAverage("7h", 7 * 3600, 30, 7 * 3600),
            WindowAverage("24h", 86400, 25, 86400),
            WindowAverage("7d", 7 * 86400, 18, 2 * 86400),
        ),
    ),
)
KEYED_GPUS = (
    GpuEntry(label="GPU card0 (radeon)", card="card0", driver="radeon", temp_c=35.0),
    GpuEntry(label="GPU card1 (amdgpu)", card="card1", driver="amdgpu", busy_percent=12),
)


def test_page_shows_the_busy_averages_under_the_matching_card():
    page = web.render_page(snapshot(gpus=KEYED_GPUS, gpu_averages=AVERAGES)).decode()

    assert "<tr><td>GPU card1 (amdgpu)</td><td>busy 12%</td></tr>" in page
    assert "<tr><td>  busy avg</td><td>1h 42% · 3h 38% · 7h 30% · 24h 25% · 7d 18% (2d)</td></tr>" in page
    assert page.index("GPU card1 (amdgpu)") < page.index("1h 42%")


def test_page_says_not_reported_for_a_card_without_averages():
    page = web.render_page(snapshot(gpus=KEYED_GPUS, gpu_averages=AVERAGES)).decode()
    radeon = page.index("GPU card0 (radeon)")
    amdgpu = page.index("GPU card1 (amdgpu)")

    assert "<td>not reported</td>" in page[radeon:amdgpu]


def test_page_gives_the_unavailable_sentinel_no_averages_row():
    page = web.render_page(snapshot(gpus=(GpuEntry(label="GPU", note="unavailable"),))).decode()

    assert "unavailable" in page
    assert "busy avg" not in page


def test_page_with_no_gpus_has_no_averages_row():
    page = web.render_page(snapshot(probed=True, gpus=(), gpu_averages=AVERAGES)).decode()

    assert "No GPU detected" in page
    assert "busy avg" not in page
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web.py -q`
Expected: the first two new tests FAIL on the missing `busy avg` row; the other two pass already (nothing renders the label yet).

- [ ] **Step 3: Render the row**

In `src/harbor_console/web.py`, add the imports:

```python
from harbor_console.gpu_history import format_averages
from harbor_console.ui import AVERAGES_LABEL, NOT_REPORTED
```

Check that `web.py` does not already import from `ui.py` in a way that would form a cycle: `ui.py` imports `checks`, `gpu`, `gpu_history`, `storage`, `verdict`; none import `web`. Fine.

Replace the `rows = ...` expression in `_gpu_section`:

```python
    by_card = {a.card: a for a in snapshot.gpu_averages}
    rows = ""
    for entry in snapshot.gpus:
        rows += f"<tr><td>{escape(entry.label)}</td><td>{escape(format_gpu(entry))}</td></tr>"
        if entry.card:
            found = by_card.get(entry.card)
            text = NOT_REPORTED if found is None else format_averages(found)
            rows += f"<tr><td>{escape(AVERAGES_LABEL)}</td><td>{escape(text)}</td></tr>"
    return "<h2>GPU</h2><table>" + rows + "</table>"
```

and extend the docstring: "Under each card, the busy averages the prober keeps (ADR 22), or `not reported` before the first cycle has recorded one."

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/web.py tests/test_web.py
git commit -m "feat: show each GPU's busy averages under its row on the status page

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: deployment, ADR 22 and the project guide

**Files:**
- Modify: `deploy/harbor-console-web.service`
- Modify: `deploy/uninstall.sh`
- Create: `docs/adr/0022-keep-a-week-of-gpu-busy-history.md`
- Modify: `CLAUDE.md`
- Test: `tests/test_deploy.py` (exists; `REPO` is its repo-root constant)

**Interfaces:** none; this task ships what the others built.

- [ ] **Step 1: Write the failing tests for the unit file and the uninstaller**

Append to `tests/test_deploy.py`:

```python
def test_the_web_unit_owns_a_state_directory_for_the_gpu_history():
    text = (REPO / "deploy" / "harbor-console-web.service").read_text(encoding="utf-8")

    assert "StateDirectory=harbor-console" in text


def test_uninstall_removes_the_state_directory():
    text = (REPO / "deploy" / "uninstall.sh").read_text(encoding="utf-8")

    assert "/var/lib/harbor-console" in text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_deploy.py -q`
Expected: FAIL on the two new tests.

- [ ] **Step 3: Edit the unit and the uninstaller**

In `deploy/harbor-console-web.service`, after the `RuntimeDirectoryPreserve=yes` line add:

```ini
# /var/lib/harbor-console, owned by harbor: the GPU busy history the
# prober keeps for a week (ADR 22). The one thing this project keeps
# across a restart or a reboot; the prober is its only writer.
StateDirectory=harbor-console
```

In `deploy/uninstall.sh`, inside the `if [[ ${PURGE} -eq 1 ]]; then` block, after `rm -rf "${INSTALL_DIR}"` add:

```bash
  echo "==> Removing /var/lib/harbor-console (GPU busy history)"
  rm -rf /var/lib/harbor-console
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_deploy.py -q`
Expected: all pass.

- [ ] **Step 5: Write ADR 22**

Create `docs/adr/0022-keep-a-week-of-gpu-busy-history.md`:

```markdown
# 22. Keep a week of GPU busy history in the web unit's state directory

Date: 2026-10-09

## Status

Accepted

## Context

The GPU row on both surfaces shows the instant: busy percent, VRAM, heat,
power, fan, read fresh each cycle. The question the row cannot answer is
whether the card was busy over the last hour, the last day, the last week
-- the one an operator sizing an inference server actually asks, and the
one the instant reading is worst at, since it is sampled once a second on
tty1 and once every 30 s on the page from a load that swings between idle
and saturated in under a minute.

An average over a week needs a week of samples, and nothing in this project
keeps anything. The founding document deferred persistence and every
module has honoured that: the console restarts on every tty1 login, the
web prober on every deploy, and the one file that crosses between them
lives in `/run`, which a reboot wipes (ADR 21). A week of history cannot
come from any process's memory.

## Decision

We will keep a week of GPU busy samples on disk, written by the web prober
and nobody else, and show five averages -- 1 h, 3 h, 7 h, 24 h, 7 d --
under each GPU row on both surfaces.

The prober appends one busy sample per card per cycle to
`/var/lib/harbor-console/gpu-history.json`, under a systemd
`StateDirectory` owned by `harbor`, prunes everything older than a week,
rewrites the file atomically, and puts the five averages into the verdict
file beside the checks. The console reads them from the file it already
reads once per tick (ADR 21) and computes nothing: zero writes, zero new
reads, nothing that can block the 1 Hz loop.

A window the samples cover less than 95% of says so beside its mean
(`7d 18% (2d)`), and a window with no samples shows a dash. Absence of
evidence is never a number (ADR 18).

Busy percent only. The other GPU metrics stay instantaneous.

## Consequences

- Both rows under a GPU say what the card has been doing, not only what it
  is doing this second.
- The project keeps one file across restarts and reboots. ADR 21's "the
  verdict file carries checks" widens to "checks and the GPU averages";
  the founding document's "no persistence" narrows to "no persistence but
  this file". A second persistent file needs a new ADR.
- The prober rewrites about 400 KB every 30 s, about 1 GB a day. Negligible
  on an SSD; accepted over batching writes, which would lose samples on a
  crash for no operational gain.
- The console spends one of its three spare rows per card (ADR 20). While
  the platform banner is up it takes all three, so the panel's bottom
  border is cropped; ADR 20's remedy, a smaller console font, applies and
  is not taken here. During the first day of a fresh history the row wraps
  once; it stops on its own.
- A lost or corrupt history file costs the week it held and nothing else:
  the keeper starts empty and the windows say how little they cover.
- Rejected: a shared history both processes write (two writers, a lock, and
  a disk write per second in the console); in-memory history (no process
  lives a week); downsampling (20k integers a week is small, and a plain
  mean over plain samples is the rule anyone can check by hand).
```

- [ ] **Step 6: Update `CLAUDE.md`**

Three edits:

1. In the architecture list, after the `gpu.py` bullet, add:

```markdown
- `gpu_history.py` — the fourth policy, pure, plus the codec for the one file this project keeps across reboots: a week of busy samples per card at `/var/lib/harbor-console/gpu-history.json`, written only by the web prober, averaged over 1 h, 3 h, 7 h, 24 h and 7 d, and formatted for both surfaces by `format_averages` ([ADR 22](docs/adr/0022-keep-a-week-of-gpu-busy-history.md)). A window the samples cover less than 95% of carries its span (`7d 18% (2d)`); one with no samples shows `—`. The averages reach the console inside the verdict file, so the 1 Hz loop gains no write and no new read.
```

2. In the "Five behaviours of that service are load-bearing" list, change the last bullet's first sentence from "**The verdict file is the only thing the console reads from the web service, and the console never probes.**" to "**The verdict file is the only thing the console reads from the web service, the console never probes, and the web prober is the only writer of the GPU history.**" and add after its last sentence: "The GPU busy averages ride in the same file (ADR 22); the console never samples, averages or writes history of its own."

3. In the Scope discipline paragraph, change "no persistence, and no user-facing configuration" to "no persistence but the GPU history file ([ADR 22](docs/adr/0022-keep-a-week-of-gpu-busy-history.md)), and no user-facing configuration".

Also change the opening description's console bullet: after "one row per GPU" insert ", each with its busy averages over 1 h, 3 h, 7 h, 24 h and 7 d beneath it".

- [ ] **Step 7: Run the whole suite one last time**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add deploy/harbor-console-web.service deploy/uninstall.sh docs/adr/0022-keep-a-week-of-gpu-busy-history.md CLAUDE.md tests/test_deploy.py
git commit -m "feat: give the web unit a state directory for the GPU history, with ADR 22

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

