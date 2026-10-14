# Memory History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep a week of memory samples (available %, PSI stall counters, OOM kill counter) written by the web prober, and show low-memory episodes, stall time and OOM kills per window on the status page, plus one summary line on the tty1 console.

**Architecture:** A file-only collector (`memory_pressure.py`) feeds a pure policy-and-codec module (`memory_history.py`, shaped like `gpu_history.py`). The web prober's keeper records, persists to `/var/lib/harbor-console/memory-history.json` and summarizes; the summary rides in the snapshot (page) and the verdict file (console). The console computes nothing but the age of the newest event against its own clock.

**Tech Stack:** Python 3.13, stdlib only (`json`, `dataclasses`, `pathlib`), `rich` for the console, stdlib `http.server` page, `pytest`. Run everything with `uv run`.

Spec: `docs/superpowers/specs/2026-10-13-memory-history-design.md`.

## Global Constraints

- No new runtime dependency. Collectors read files only; no subprocess.
- Collectors and `loads`/`read_history` never raise. `write_history` raises `OSError`; the keeper reports it.
- Windows are `gpu_history.WINDOWS` (`1h`, `3h`, `7h`, `24h`, `7d`), imported, not copied.
- `LOW_AVAILABLE_PERCENT = 10.0`, `MAX_SAMPLE_GAP = 120 s`, `BOOT_TOLERANCE = 60 s`, `RETENTION = 7 d`, `RECENT_EVENTS = 5`, coverage flag below `gpu_history.FULL_COVERAGE` (0.95). Constants, no configuration.
- Unknown is `None` in data and `—` (`gpu_history.NO_MEAN`) on screen; never zero. Absence of evidence is never a finding.
- No colour, no banner change.
- Console value width is 54 cells; the fullest console line must fit.
- Tests use plain values, fixed datetimes, no real time/sockets/disk except `tmp_path`.
- Commits end with the trailer `Assisted-by: Claude Opus 5.5 (Claude Code)` and no `Co-Authored-By` line. Do not chain shell commands with `&&`.
- Work on branch `feat/memory-history` (already checked out).

---

### Task 1: Collector `memory_pressure.py`

**Files:**
- Create: `src/harbor_console/memory_pressure.py`
- Test: `tests/test_memory_pressure.py`

**Interfaces:**
- Produces: `MemoryReading(available_percent: float | None = None, stall_some_us: int | None = None, stall_full_us: int | None = None, oom_kills: int | None = None, boot: int | None = None)` (frozen dataclass); `collect_memory_reading(read_text: Callable[[Path], str] = _read_file) -> MemoryReading`; path constants `MEMINFO`, `PRESSURE`, `VMSTAT`, `STAT`.

- [ ] **Step 1: Write the failing tests**

```python
"""The memory collector reads four /proc files and never raises."""

from pathlib import Path

from harbor_console.memory_pressure import (
    MEMINFO,
    PRESSURE,
    STAT,
    VMSTAT,
    MemoryReading,
    collect_memory_reading,
)

MEMINFO_TEXT = "MemTotal:       65755136 kB\nMemFree:        36649984 kB\nMemAvailable:   50948096 kB\n"
PRESSURE_TEXT = (
    "some avg10=0.00 avg60=0.00 avg300=0.00 total=396806\n"
    "full avg10=0.00 avg60=0.00 avg300=0.00 total=394230\n"
)
VMSTAT_TEXT = "pgfault 123\noom_kill 2\npgmajfault 4\n"
STAT_TEXT = "cpu  1 2 3 4\nbtime 1760000000\nprocesses 99\n"

FILES = {MEMINFO: MEMINFO_TEXT, PRESSURE: PRESSURE_TEXT, VMSTAT: VMSTAT_TEXT, STAT: STAT_TEXT}


def reader(files: dict[Path, str]):
    def read(path: Path) -> str:
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]

    return read


def test_reads_every_field():
    assert collect_memory_reading(reader(FILES)) == MemoryReading(
        available_percent=77.5,
        stall_some_us=396806,
        stall_full_us=394230,
        oom_kills=2,
        boot=1760000000,
    )


def test_a_missing_pressure_file_leaves_only_the_stall_fields_unknown():
    files = {k: v for k, v in FILES.items() if k != PRESSURE}
    reading = collect_memory_reading(reader(files))
    assert reading.stall_some_us is None
    assert reading.stall_full_us is None
    assert reading.available_percent == 77.5
    assert reading.oom_kills == 2
    assert reading.boot == 1760000000


def test_nothing_readable_is_all_unknown():
    assert collect_memory_reading(reader({})) == MemoryReading()


def test_garbled_files_are_unknown_not_errors():
    files = {
        MEMINFO: "MemTotal: lots kB\nMemAvailable: 1 kB\n",
        PRESSURE: "some avg10=0.00 total=many\nfull\n",
        VMSTAT: "oom_kill -3\n",
        STAT: "btime\n",
    }
    assert collect_memory_reading(reader(files)) == MemoryReading()


def test_a_zero_total_is_unknown_not_a_division_error():
    files = dict(FILES)
    files[MEMINFO] = "MemTotal: 0 kB\nMemAvailable: 0 kB\n"
    assert collect_memory_reading(reader(files)).available_percent is None


def test_a_read_that_raises_anything_os_level_is_unknown():
    def read(path: Path) -> str:
        raise PermissionError(path)

    assert collect_memory_reading(read) == MemoryReading()


def test_undecodable_bytes_are_unknown():
    def read(path: Path) -> str:
        raise UnicodeDecodeError("ascii", b"\xff", 0, 1, "bad")

    assert collect_memory_reading(read) == MemoryReading()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_memory_pressure.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'harbor_console.memory_pressure'`.

- [ ] **Step 3: Implement**

```python
"""Collects how short of memory this host is, from four /proc files.

Files only, no subprocess, nothing that can hang: the web prober calls this
every cycle (ADR 23). Every field is optional and each file is read and
parsed on its own, so a kernel without PSI still reports available memory
and OOM kills.

- `/proc/meminfo` -- `MemAvailable` as a share of `MemTotal`.
- `/proc/pressure/memory` -- the cumulative `some` and `full` stall times in
  microseconds. Cumulative is the point: the delta between two samples
  counts every stall between them, however brief.
- `/proc/vmstat` -- `oom_kill`, cumulative since boot, same reason.
- `/proc/stat` -- `btime`, so a counter that restarted with the host can be
  told from one that did not move.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MEMINFO = Path("/proc/meminfo")
PRESSURE = Path("/proc/pressure/memory")
VMSTAT = Path("/proc/vmstat")
STAT = Path("/proc/stat")


@dataclass(frozen=True)
class MemoryReading:
    """One look at the four files. `None` is "could not read", never zero."""

    available_percent: float | None = None
    stall_some_us: int | None = None
    stall_full_us: int | None = None
    oom_kills: int | None = None
    boot: int | None = None


def _read_file(path: Path) -> str:
    return path.read_text(encoding="ascii")


def collect_memory_reading(read_text: Callable[[Path], str] = _read_file) -> MemoryReading:
    """Read every file once. Never raises."""
    meminfo = _read(read_text, MEMINFO)
    pressure = _read(read_text, PRESSURE)
    vmstat = _read(read_text, VMSTAT)
    stat = _read(read_text, STAT)
    return MemoryReading(
        available_percent=_available_percent(meminfo),
        stall_some_us=_stall_total(pressure, "some"),
        stall_full_us=_stall_total(pressure, "full"),
        oom_kills=_counter(vmstat, "oom_kill"),
        boot=_counter(stat, "btime"),
    )


def _read(read_text: Callable[[Path], str], path: Path) -> str | None:
    try:
        return read_text(path)
    except (OSError, ValueError):  # UnicodeDecodeError is a ValueError
        return None


def _fields(text: str) -> dict[str, str]:
    """`name value ...` lines to `{name: value}`; a trailing `:` on the name
    (meminfo's style) is dropped."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            fields[parts[0].rstrip(":")] = parts[1]
    return fields


def _non_negative(value: str) -> int | None:
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number >= 0 else None


def _counter(text: str | None, name: str) -> int | None:
    if text is None:
        return None
    value = _fields(text).get(name)
    return None if value is None else _non_negative(value)


def _available_percent(text: str | None) -> float | None:
    if text is None:
        return None
    fields = _fields(text)
    total = _non_negative(fields.get("MemTotal", ""))
    available = _non_negative(fields.get("MemAvailable", ""))
    if total is None or available is None or total == 0:
        return None
    return round(available * 100 / total, 1)


def _stall_total(text: str | None, kind: str) -> int | None:
    if text is None:
        return None
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == kind:
            for part in parts[1:]:
                if part.startswith("total="):
                    return _non_negative(part.removeprefix("total="))
    return None
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_memory_pressure.py -q`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/memory_pressure.py tests/test_memory_pressure.py
git commit -m "feat: collect available memory, PSI stall totals and OOM kills from /proc

Assisted-by: Claude Opus 5.5 (Claude Code)"
```

---

### Task 2: `memory_history.py` -- samples, record, file codec

**Files:**
- Create: `src/harbor_console/memory_history.py`
- Test: `tests/test_memory_history.py`

**Interfaces:**
- Consumes: `MemoryReading` from Task 1.
- Produces: `Sample(at: datetime, available_percent: float | None, stall_some_us: int | None, stall_full_us: int | None, oom_kills: int | None, boot: int | None)`; `History = tuple[Sample, ...]`; `record(history: History, reading: MemoryReading, now: datetime) -> History`; `dumps(history) -> str`; `loads(text) -> History`; `read_history(path: Path = HISTORY_PATH) -> History`; `write_history(history: History, path: Path = HISTORY_PATH) -> None`; constants `RETENTION`, `HISTORY_PATH`, `LOW_AVAILABLE_PERCENT`, `MAX_SAMPLE_GAP`, `BOOT_TOLERANCE`, `RECENT_EVENTS`.

- [ ] **Step 1: Write the failing tests**

```python
"""A week of memory samples: record, prune, codec."""

from datetime import datetime, timedelta

from harbor_console.memory_history import (
    RETENTION,
    Sample,
    dumps,
    loads,
    read_history,
    record,
    write_history,
)
from harbor_console.memory_pressure import MemoryReading

NOW = datetime(2026, 10, 13, 12, 0, 0)
READING = MemoryReading(
    available_percent=42.5, stall_some_us=10, stall_full_us=5, oom_kills=0, boot=1760000000
)


def sample(at: datetime, available: float | None = 50.0, some=0, full=0, oom=0, boot=1760000000) -> Sample:
    return Sample(at, available, some, full, oom, boot)


def test_record_appends_one_sample_at_now():
    assert record((), READING, NOW) == (Sample(NOW, 42.5, 10, 5, 0, 1760000000),)


def test_record_keeps_a_sample_that_read_nothing():
    assert record((), MemoryReading(), NOW) == (Sample(NOW, None, None, None, None, None),)


def test_record_prunes_samples_older_than_retention():
    old = sample(NOW - RETENTION - timedelta(seconds=1))
    edge = sample(NOW - RETENTION)
    assert record((old, edge), READING, NOW)[:1] == (edge,)
    assert len(record((old, edge), READING, NOW)) == 2


def test_record_never_mutates_its_input():
    history = (sample(NOW - timedelta(minutes=1)),)
    record(history, READING, NOW)
    assert history == (sample(NOW - timedelta(minutes=1)),)


def test_codec_round_trip():
    history = (
        sample(NOW - timedelta(seconds=30), available=7.3, some=100, full=50, oom=1),
        Sample(NOW, None, None, None, None, None),
    )
    assert loads(dumps(history)) == history


def test_dumps_is_one_compact_line():
    text = dumps((sample(NOW),))
    assert "\n" not in text
    assert " " not in text


def test_loads_garbage_is_empty():
    for text in [
        "",
        "not json",
        "[]",
        '{"samples": {}}',
        '{"samples": [[1, 2]]}',
        '{"samples": [["x", 1, 1, 1, 1, 1]]}',
        '{"samples": [[1, 101, 1, 1, 1, 1]]}',
        '{"samples": [[1, 5, -1, 1, 1, 1]]}',
        '{"samples": [[1, true, 1, 1, 1, 1]]}',
        '{"samples": [[1, 5, 1.5, 1, 1, 1]]}',
    ]:
        assert loads(text) == (), text


def test_read_history_of_a_missing_file_is_empty(tmp_path):
    assert read_history(tmp_path / "missing.json") == ()


def test_write_then_read_leaves_no_temp_file(tmp_path):
    path = tmp_path / "memory-history.json"
    history = (sample(NOW),)
    write_history(history, path)
    assert read_history(path) == history
    assert [p.name for p in tmp_path.iterdir()] == ["memory-history.json"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_memory_history.py -q`
Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
"""A week of memory samples, and what both surfaces show from them.

Policy and codec, in the shape of `gpu_history.py`. The web prober is the
one writer: each cycle it records one sample, prunes what is older than a
week, and hands the summary to the snapshot and the verdict file (ADR 23).
The console only ever sees the summary, inside the file it already reads.

Three signals over one timeline: low episodes (available memory under
`LOW_AVAILABLE_PERCENT`, sampled), and stall time and OOM kills from the
kernel's cumulative counters, whose deltas cannot miss what happened
between two samples. Raw samples are kept so these rules can change
without losing the week.

Everything here is pure in `now`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from harbor_console.memory_pressure import MemoryReading

#: Samples older than this are dropped on every record.
RETENTION = timedelta(days=7)

#: A sample is low when available memory is under this share of total.
LOW_AVAILABLE_PERCENT = 10.0

#: Two low samples further apart than this are two episodes: the prober or
#: the host was down between them, and nobody saw what happened.
MAX_SAMPLE_GAP = timedelta(seconds=120)

#: `btime` is the wall clock minus uptime, so NTP can move it a second or
#: two without a reboot. Boots closer than this are the same boot.
BOOT_TOLERANCE = 60

#: How many recent events the summary carries.
RECENT_EVENTS = 5

#: Beside the GPU history, under the web unit's `StateDirectory` (ADR 23).
HISTORY_PATH = Path("/var/lib/harbor-console/memory-history.json")


@dataclass(frozen=True)
class Sample:
    at: datetime
    available_percent: float | None
    stall_some_us: int | None
    stall_full_us: int | None
    oom_kills: int | None
    boot: int | None


#: Oldest first.
History = tuple[Sample, ...]


def record(history: History, reading: MemoryReading, now: datetime) -> History:
    """`history` plus one sample at `now`, minus everything older than
    `RETENTION`. A sample that read nothing is still kept: it is a cycle
    that looked, and it breaks a low run rather than extending it."""
    cutoff = now - RETENTION
    sample = Sample(
        now,
        reading.available_percent,
        reading.stall_some_us,
        reading.stall_full_us,
        reading.oom_kills,
        reading.boot,
    )
    return tuple(s for s in (*history, sample) if s.at >= cutoff)


def dumps(history: History) -> str:
    """`{"samples": [[epoch, available, some_us, full_us, oom_kills, boot], ...]}`
    on one line, `null` for a field that was not read."""
    return json.dumps(
        {
            "samples": [
                [
                    int(s.at.timestamp()),
                    s.available_percent,
                    s.stall_some_us,
                    s.stall_full_us,
                    s.oom_kills,
                    s.boot,
                ]
                for s in history
            ]
        },
        separators=(",", ":"),
    )


def loads(text: str) -> History:
    """Parse a history, or an empty one for anything that is not one.
    Never raises."""
    try:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            return ()
        rows = payload["samples"]
        if not isinstance(rows, list):
            return ()
        samples = []
        for row in rows:
            if not (isinstance(row, list) and len(row) == 6):
                return ()
            at, available, some, full, oom, boot = row
            if not _is_int(at):
                return ()
            if available is not None and not (_is_number(available) and 0 <= available <= 100):
                return ()
            if not all(v is None or (_is_int(v) and v >= 0) for v in (some, full, oom, boot)):
                return ()
            samples.append(
                Sample(
                    datetime.fromtimestamp(at),
                    None if available is None else float(available),
                    some,
                    full,
                    oom,
                    boot,
                )
            )
        return tuple(samples)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return ()


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def read_history(path: Path = HISTORY_PATH) -> History:
    """The history on disk, or an empty one. Never raises."""
    try:
        return loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()


def write_history(history: History, path: Path = HISTORY_PATH) -> None:
    """Replace the file atomically. Raises OSError; the caller reports it."""
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(dumps(history), encoding="utf-8")
    os.replace(temp, path)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_memory_history.py -q`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/memory_history.py tests/test_memory_history.py
git commit -m "feat: record a week of memory samples and keep them in a file

Assisted-by: Claude Opus 5.5 (Claude Code)"
```

---

### Task 3: `memory_history.py` -- summarize (windows, episodes, counter deltas, events)

**Files:**
- Modify: `src/harbor_console/memory_history.py` (append)
- Test: `tests/test_memory_history.py` (append)

**Interfaces:**
- Consumes: `Sample`, `History`, constants from Task 2; `gpu_history.WINDOWS`.
- Produces:
  - `MemoryWindow(window: str, seconds: int, min_available: float | None, low_episodes: int, stall_some_seconds: float | None, stall_full_seconds: float | None, oom_kills: int | None, covered_seconds: int)`
  - `MemoryEvent(kind: str, at: datetime, duration_seconds: int, min_available: float | None, count: int)`, `kind` is `EVENT_LOW = "low"` or `EVENT_OOM = "oom"`
  - `MemorySummary(windows: tuple[MemoryWindow, ...], events: tuple[MemoryEvent, ...])`
  - `summarize(history: History, now: datetime) -> MemorySummary`

- [ ] **Step 1: Write the failing tests** (append; add `EVENT_LOW, EVENT_OOM, MemoryEvent, summarize` to the import list at the top of the file)

```python
def at(seconds_ago: int) -> datetime:
    return NOW - timedelta(seconds=seconds_ago)


def window(summary, name):
    return next(w for w in summary.windows if w.window == name)


def test_empty_history_has_every_window_unknown():
    summary = summarize((), NOW)
    assert [w.window for w in summary.windows] == ["1h", "3h", "7h", "24h", "7d"]
    for w in summary.windows:
        assert (w.min_available, w.low_episodes, w.stall_some_seconds, w.stall_full_seconds, w.oom_kills, w.covered_seconds) == (
            None, 0, None, None, None, 0,
        )
    assert summary.events == ()


def test_min_available_and_coverage():
    history = (sample(at(7200), available=30.0), sample(at(60), available=12.5), sample(at(0), available=20.0))
    summary = summarize(history, NOW)
    assert window(summary, "1h").min_available == 12.5
    assert window(summary, "1h").covered_seconds == 60
    assert window(summary, "3h").min_available == 12.5
    assert window(summary, "3h").covered_seconds == 7200


def test_consecutive_low_samples_are_one_episode():
    history = (
        sample(at(120), available=50.0),
        sample(at(90), available=9.0),
        sample(at(60), available=6.2),
        sample(at(30), available=8.0),
        sample(at(0), available=50.0),
    )
    summary = summarize(history, NOW)
    assert window(summary, "1h").low_episodes == 1
    assert summary.events == (MemoryEvent(EVENT_LOW, at(90), 60, 6.2, 1),)


def test_a_recovered_sample_splits_episodes():
    history = (sample(at(90), available=5.0), sample(at(60), available=50.0), sample(at(30), available=5.0))
    assert window(summarize(history, NOW), "1h").low_episodes == 2


def test_an_unread_sample_splits_episodes():
    history = (sample(at(90), available=5.0), sample(at(60), available=None), sample(at(30), available=5.0))
    assert window(summarize(history, NOW), "1h").low_episodes == 2


def test_a_gap_over_the_limit_splits_episodes():
    history = (sample(at(300), available=5.0), sample(at(150), available=5.0))
    assert window(summarize(history, NOW), "1h").low_episodes == 2


def test_exactly_ten_percent_is_not_low():
    assert window(summarize((sample(at(0), available=10.0),), NOW), "1h").low_episodes == 0


def test_an_episode_counts_in_a_window_when_any_sample_is_inside():
    history = (sample(at(3660), available=5.0), sample(at(3600), available=5.0))
    assert window(summarize(history, NOW), "1h").low_episodes == 1


def test_counter_deltas_sum_into_windows():
    history = (
        sample(at(7200), some=1_000_000, full=0, oom=0),
        sample(at(60), some=3_000_000, full=500_000, oom=1),
        sample(at(0), some=3_500_000, full=500_000, oom=3),
    )
    summary = summarize(history, NOW)
    one_hour = window(summary, "1h")
    assert one_hour.stall_some_seconds == 2.5
    assert one_hour.stall_full_seconds == 0.5
    assert one_hour.oom_kills == 3


def test_a_steady_counter_is_zero_not_unknown():
    history = (sample(at(30), oom=4), sample(at(0), oom=4))
    assert window(summarize(history, NOW), "1h").oom_kills == 0


def test_a_counter_that_went_down_is_a_reboot():
    history = (sample(at(30), oom=9, boot=1), sample(at(0), oom=2, boot=1))
    assert window(summarize(history, NOW), "1h").oom_kills == 2


def test_a_changed_boot_is_a_reboot():
    history = (sample(at(30), oom=1, boot=1760000000), sample(at(0), oom=3, boot=1760090000))
    assert window(summarize(history, NOW), "1h").oom_kills == 3


def test_boot_jitter_within_tolerance_is_the_same_boot():
    history = (sample(at(30), oom=1, boot=1760000000), sample(at(0), oom=3, boot=1760000002))
    assert window(summarize(history, NOW), "1h").oom_kills == 2


def test_a_window_with_no_usable_pair_is_unknown():
    history = (sample(at(30), oom=None), sample(at(0), oom=4))
    assert window(summarize(history, NOW), "1h").oom_kills is None


def test_an_oom_delta_is_an_event_at_the_later_sample():
    history = (sample(at(30), oom=0), sample(at(0), oom=2))
    assert summarize(history, NOW).events == (MemoryEvent(EVENT_OOM, at(0), 0, None, 2),)


def test_events_are_newest_first_and_capped():
    history = tuple(sample(at(600 - 60 * i), available=5.0 if i % 2 == 0 else 50.0) for i in range(11))
    events = summarize(history, NOW).events
    assert len(events) == 5
    assert [e.at for e in events] == sorted((e.at for e in events), reverse=True)
    assert events[0].at == at(0)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_memory_history.py -q`
Expected: FAIL, `ImportError: cannot import name 'EVENT_LOW'`.

- [ ] **Step 3: Implement** (append to `memory_history.py`; add `from harbor_console.gpu_history import WINDOWS` to the imports)

```python
EVENT_LOW = "low"
EVENT_OOM = "oom"


@dataclass(frozen=True)
class MemoryWindow:
    """One window. `None` is unknown: no sample, or no pair of consecutive
    samples that both read that counter. `covered_seconds` is `now` minus
    the oldest sample inside the window, as in `gpu_history`."""

    window: str
    seconds: int
    min_available: float | None
    low_episodes: int
    stall_some_seconds: float | None
    stall_full_seconds: float | None
    oom_kills: int | None
    covered_seconds: int


@dataclass(frozen=True)
class MemoryEvent:
    """A low episode (`at` its first sample, `duration_seconds` to its last,
    `count` 1) or a cycle whose OOM counter rose (`at` that sample,
    `count` the kills, `min_available` None)."""

    kind: str
    at: datetime
    duration_seconds: int
    min_available: float | None
    count: int


@dataclass(frozen=True)
class MemorySummary:
    windows: tuple[MemoryWindow, ...]
    events: tuple[MemoryEvent, ...]


def summarize(history: History, now: datetime) -> MemorySummary:
    episodes = _episodes(history)
    some = _deltas(history, "stall_some_us")
    full = _deltas(history, "stall_full_us")
    ooms = _deltas(history, "oom_kills")
    windows = tuple(
        _window(history, episodes, some, full, ooms, name, seconds, now) for name, seconds in WINDOWS
    )
    week_start = now - timedelta(seconds=WINDOWS[-1][1])
    events = [
        MemoryEvent(
            EVENT_LOW,
            run[0].at,
            int((run[-1].at - run[0].at).total_seconds()),
            min(s.available_percent for s in run if s.available_percent is not None),
            1,
        )
        for run in episodes
        if any(week_start <= s.at <= now for s in run)
    ]
    events += [
        MemoryEvent(EVENT_OOM, when, 0, None, kills)
        for when, kills in ooms
        if kills > 0 and week_start <= when <= now
    ]
    events.sort(key=lambda e: e.at, reverse=True)
    return MemorySummary(windows, tuple(events[:RECENT_EVENTS]))


def _is_low(sample: Sample) -> bool:
    return sample.available_percent is not None and sample.available_percent < LOW_AVAILABLE_PERCENT


def _episodes(history: History) -> list[tuple[Sample, ...]]:
    """Maximal runs of consecutive low samples no more than `MAX_SAMPLE_GAP`
    apart. A sample that is not low, or did not read, ends a run."""
    runs: list[tuple[Sample, ...]] = []
    current: list[Sample] = []
    for sample in history:
        if not _is_low(sample):
            if current:
                runs.append(tuple(current))
            current = []
        elif current and sample.at - current[-1].at <= MAX_SAMPLE_GAP:
            current.append(sample)
        else:
            if current:
                runs.append(tuple(current))
            current = [sample]
    if current:
        runs.append(tuple(current))
    return runs


def _deltas(history: History, field: str) -> list[tuple[datetime, int]]:
    """Per pair of consecutive samples that both read `field`: what the
    counter added between them, credited at the later sample. A reboot
    (boots further apart than `BOOT_TOLERANCE`, or a counter that went
    down) restarts the counter from zero, so the delta is the later value."""
    deltas = []
    for earlier, later in zip(history, history[1:]):
        before, after = getattr(earlier, field), getattr(later, field)
        if before is None or after is None:
            continue
        rebooted = after < before or (
            earlier.boot is not None
            and later.boot is not None
            and abs(later.boot - earlier.boot) > BOOT_TOLERANCE
        )
        deltas.append((later.at, after if rebooted else after - before))
    return deltas


def _window(
    history: History,
    episodes: list[tuple[Sample, ...]],
    some: list[tuple[datetime, int]],
    full: list[tuple[datetime, int]],
    ooms: list[tuple[datetime, int]],
    name: str,
    seconds: int,
    now: datetime,
) -> MemoryWindow:
    start = now - timedelta(seconds=seconds)

    def inside(when: datetime) -> bool:
        return start <= when <= now

    samples = [s for s in history if inside(s.at)]
    available = [s.available_percent for s in samples if s.available_percent is not None]

    def total(deltas: list[tuple[datetime, int]]) -> int | None:
        picked = [d for when, d in deltas if inside(when)]
        return sum(picked) if picked else None

    some_us, full_us = total(some), total(full)
    return MemoryWindow(
        window=name,
        seconds=seconds,
        min_available=min(available) if available else None,
        low_episodes=sum(1 for run in episodes if any(inside(s.at) for s in run)),
        stall_some_seconds=None if some_us is None else some_us / 1_000_000,
        stall_full_seconds=None if full_us is None else full_us / 1_000_000,
        oom_kills=total(ooms),
        covered_seconds=int((now - min(s.at for s in samples)).total_seconds()) if samples else 0,
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_memory_history.py -q`
Expected: all passed (10 from Task 2 plus 16 new).

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/memory_history.py tests/test_memory_history.py
git commit -m "feat: summarize memory history into low episodes, stall time and OOM kills

Assisted-by: Claude Opus 5.5 (Claude Code)"
```

---

### Task 4: `memory_history.py` -- formatting and the summary's JSON form

**Files:**
- Modify: `src/harbor_console/memory_history.py` (append)
- Test: `tests/test_memory_history.py` (append)

**Interfaces:**
- Consumes: Task 3 types; `gpu_history.FULL_COVERAGE`, `gpu_history.NO_MEAN`, `gpu_history.WINDOWS`.
- Produces:
  - `CONSOLE_LABEL = "Mem events"`, `WINDOW_HEADERS = ("window", "min avail", "low", "stall some", "stall full", "oom")`, `NO_EVENTS = "No low-memory events this week."`
  - `format_duration(seconds: float) -> str`
  - `format_console_line(summary: MemorySummary, now: datetime | None) -> str`
  - `window_cells(w: MemoryWindow) -> tuple[str, str, str, str, str, str]`
  - `format_event(event: MemoryEvent) -> str`
  - `summary_to_json(summary: MemorySummary) -> dict`
  - `summary_from_json(raw: object) -> MemorySummary` (raises `ValueError`/`KeyError`/`TypeError` on anything malformed)

- [ ] **Step 1: Write the failing tests** (append; extend the import list with `MemorySummary, MemoryWindow, format_console_line, format_duration, format_event, summary_from_json, summary_to_json, window_cells`; add `import pytest` at the top)

```python
WEEK = 7 * 86400


def full_week(**overrides) -> MemoryWindow:
    values = dict(
        window="7d", seconds=WEEK, min_available=6.2, low_episodes=3,
        stall_some_seconds=30.0, stall_full_seconds=12.4, oom_kills=1, covered_seconds=WEEK,
    )
    values.update(overrides)
    return MemoryWindow(**values)


def summary_with(week: MemoryWindow, events=()) -> MemorySummary:
    others = tuple(
        MemoryWindow(name, secs, None, 0, None, None, None, 0)
        for name, secs in [("1h", 3600), ("3h", 10800), ("7h", 25200), ("24h", 86400)]
    )
    return MemorySummary(others + (week,), tuple(events))


def test_format_duration_units():
    assert [format_duration(s) for s in (0, 12.4, 59, 60, 3599, 3600, 86399, 86400, 200000)] == [
        "0s", "12s", "59s", "1m", "59m", "1h", "23h", "1d", "2d",
    ]


def test_console_line_with_an_event():
    event = MemoryEvent(EVENT_LOW, NOW - timedelta(hours=2), 600, 6.2, 1)
    assert format_console_line(summary_with(full_week(), [event]), NOW) == (
        "7d: 3 low · last 2h ago · stall 12s · oom 1"
    )


def test_console_line_without_events_omits_last():
    week = full_week(low_episodes=0, stall_full_seconds=0.0, oom_kills=0)
    assert format_console_line(summary_with(week), NOW) == "7d: 0 low · stall 0s · oom 0"


def test_console_line_without_a_clock_omits_last():
    event = MemoryEvent(EVENT_OOM, NOW, 0, None, 1)
    assert "last" not in format_console_line(summary_with(full_week(), [event]), None)


def test_console_line_unknown_counters_are_dashes():
    week = full_week(stall_full_seconds=None, oom_kills=None)
    assert format_console_line(summary_with(week), NOW) == "7d: 3 low · stall — · oom —"


def test_console_line_flags_a_partly_covered_week():
    week = full_week(covered_seconds=2 * 86400 + 5)
    assert format_console_line(summary_with(week), NOW).startswith("7d (2d): ")


def test_console_line_never_reports_a_negative_age():
    event = MemoryEvent(EVENT_OOM, NOW + timedelta(minutes=5), 0, None, 1)
    assert "last 0s ago" in format_console_line(summary_with(full_week(), [event]), NOW)


def test_fullest_console_line_fits_the_value_column():
    week = full_week(low_episodes=99, stall_full_seconds=3599, oom_kills=99, covered_seconds=6 * 86400 + 1)
    event = MemoryEvent(EVENT_LOW, NOW - timedelta(hours=23, minutes=59), 0, 1.0, 1)
    line = format_console_line(summary_with(week, [event]), NOW)
    assert line == "7d (6d): 99 low · last 23h ago · stall 59m · oom 99"
    assert len(line) <= 54


def test_window_cells():
    assert window_cells(full_week()) == ("7d", "6.2%", "3", "30s", "12s", "1")
    assert window_cells(MemoryWindow("1h", 3600, None, 0, None, None, None, 0)) == (
        "1h", "—", "0", "—", "—", "—",
    )
    assert window_cells(MemoryWindow("1h", 3600, 50.0, 0, 0.0, 0.0, 0, 1200))[0] == "1h (20m)"


def test_format_event():
    low = MemoryEvent(EVENT_LOW, datetime(2026, 10, 13, 14, 2), 720, 6.2, 1)
    oom = MemoryEvent(EVENT_OOM, datetime(2026, 10, 13, 14, 14), 0, None, 1)
    assert format_event(low) == "2026-10-13 14:02 low 12m, min 6.2%"
    assert format_event(oom) == "2026-10-13 14:14 oom ×1"


def test_summary_json_round_trip():
    events = [
        MemoryEvent(EVENT_LOW, NOW - timedelta(hours=2), 600, 6.2, 1),
        MemoryEvent(EVENT_OOM, NOW, 0, None, 2),
    ]
    summary = summary_with(full_week(), events)
    assert summary_from_json(summary_to_json(summary)) == summary


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(windows="x"),
        lambda d: d["windows"][0].update(window="2h"),
        lambda d: d["windows"][0].update(low_episodes=-1),
        lambda d: d["windows"][0].update(min_available=120),
        lambda d: d["windows"][0].update(oom_kills=True),
        lambda d: d["events"].append({"kind": "boom", "at": NOW.isoformat(), "duration_seconds": 0, "min_available": None, "count": 1}),
        lambda d: d.pop("events"),
    ],
)
def test_summary_from_json_rejects_malformed(mutate):
    raw = summary_to_json(summary_with(full_week()))
    mutate(raw)
    with pytest.raises((ValueError, KeyError, TypeError)):
        summary_from_json(raw)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_memory_history.py -q`
Expected: FAIL, `ImportError: cannot import name 'format_console_line'`.

- [ ] **Step 3: Implement** (append; change the `gpu_history` import to `from harbor_console.gpu_history import FULL_COVERAGE, NO_MEAN, WINDOWS`)

```python
#: The console row's label.
CONSOLE_LABEL = "Mem events"
#: The page table's columns, in `window_cells` order.
WINDOW_HEADERS = ("window", "min avail", "low", "stall some", "stall full", "oom")
#: The page's events list when the week has none.
NO_EVENTS = "No low-memory events this week."


def format_duration(seconds: float) -> str:
    """`45s`, `12m`, `5h`, `2d`: floored to the coarsest whole unit."""
    whole = int(seconds)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m"
    if whole < 86400:
        return f"{whole // 3600}h"
    return f"{whole // 86400}d"


def _window_name(w: MemoryWindow) -> str:
    """The window, with the span its samples cover when that is under 95%
    of it. A window with no samples (covered 0) carries no span: its cells
    already say `—`, as the GPU row does."""
    if 0 < w.covered_seconds < FULL_COVERAGE * w.seconds:
        return f"{w.window} ({format_duration(w.covered_seconds)})"
    return w.window


def _seconds_or_unknown(value: float | None) -> str:
    return NO_MEAN if value is None else format_duration(value)


def _count_or_unknown(value: int | None) -> str:
    return NO_MEAN if value is None else str(value)


def format_console_line(summary: MemorySummary, now: datetime | None) -> str:
    """`7d: 3 low · last 2h ago · stall 12s · oom 1`, from the week.

    `stall` is the `full` stall, the one where every task waited. `last …
    ago` is the newest event of either kind, against the reader's own
    clock, and is omitted with no event or no clock.
    """
    week = summary.windows[-1]
    parts = [f"{_window_name(week)}: {week.low_episodes} low"]
    if summary.events and now is not None:
        age = max(0.0, (now - summary.events[0].at).total_seconds())
        parts.append(f"last {format_duration(age)} ago")
    parts.append(f"stall {_seconds_or_unknown(week.stall_full_seconds)}")
    parts.append(f"oom {_count_or_unknown(week.oom_kills)}")
    return " · ".join(parts)


def window_cells(w: MemoryWindow) -> tuple[str, str, str, str, str, str]:
    """One page-table row, in `WINDOW_HEADERS` order."""
    return (
        _window_name(w),
        NO_MEAN if w.min_available is None else f"{w.min_available:.1f}%",
        str(w.low_episodes),
        _seconds_or_unknown(w.stall_some_seconds),
        _seconds_or_unknown(w.stall_full_seconds),
        _count_or_unknown(w.oom_kills),
    )


def format_event(event: MemoryEvent) -> str:
    """`2026-10-13 14:02 low 12m, min 6.2%` or `2026-10-13 14:14 oom ×1`."""
    stamp = event.at.strftime("%Y-%m-%d %H:%M")
    if event.kind == EVENT_OOM:
        return f"{stamp} oom ×{event.count}"
    minimum = NO_MEAN if event.min_available is None else f"{event.min_available:.1f}%"
    return f"{stamp} low {format_duration(event.duration_seconds)}, min {minimum}"


def summary_to_json(summary: MemorySummary) -> dict:
    """The verdict file's `memory` value."""
    return {
        "windows": [
            {
                "window": w.window,
                "seconds": w.seconds,
                "min_available": w.min_available,
                "low_episodes": w.low_episodes,
                "stall_some_seconds": w.stall_some_seconds,
                "stall_full_seconds": w.stall_full_seconds,
                "oom_kills": w.oom_kills,
                "covered_seconds": w.covered_seconds,
            }
            for w in summary.windows
        ],
        "events": [
            {
                "kind": e.kind,
                "at": e.at.isoformat(),
                "duration_seconds": e.duration_seconds,
                "min_available": e.min_available,
                "count": e.count,
            }
            for e in summary.events
        ],
    }


def summary_from_json(raw: object) -> MemorySummary:
    """The inverse of `summary_to_json`. Raises on anything malformed; the
    verdict codec turns that into "no verdict"."""
    if not isinstance(raw, dict) or not isinstance(raw["windows"], list) or not isinstance(raw["events"], list):
        raise ValueError("malformed memory summary")
    windows = []
    for w in raw["windows"]:
        if not isinstance(w, dict):
            raise ValueError("malformed memory window")
        name, seconds, covered = w["window"], w["seconds"], w["covered_seconds"]
        if not (isinstance(name, str) and _is_int(seconds) and _is_int(covered)):
            raise ValueError("malformed memory window")
        if (name, seconds) not in WINDOWS or not 0 <= covered <= seconds:
            raise ValueError("malformed memory window")
        minimum = w["min_available"]
        if minimum is not None and not (_is_number(minimum) and 0 <= minimum <= 100):
            raise ValueError("malformed memory window")
        low = w["low_episodes"]
        if not (_is_int(low) and low >= 0):
            raise ValueError("malformed memory window")
        some, full, oom = w["stall_some_seconds"], w["stall_full_seconds"], w["oom_kills"]
        if not all(v is None or (_is_number(v) and v >= 0) for v in (some, full)):
            raise ValueError("malformed memory window")
        if oom is not None and not (_is_int(oom) and oom >= 0):
            raise ValueError("malformed memory window")
        windows.append(
            MemoryWindow(
                name,
                seconds,
                None if minimum is None else float(minimum),
                low,
                None if some is None else float(some),
                None if full is None else float(full),
                oom,
                covered,
            )
        )
    events = []
    for e in raw["events"]:
        if not isinstance(e, dict) or e["kind"] not in (EVENT_LOW, EVENT_OOM):
            raise ValueError("malformed memory event")
        duration, count, minimum = e["duration_seconds"], e["count"], e["min_available"]
        if not (_is_int(duration) and duration >= 0 and _is_int(count) and count >= 0):
            raise ValueError("malformed memory event")
        if minimum is not None and not (_is_number(minimum) and 0 <= minimum <= 100):
            raise ValueError("malformed memory event")
        events.append(
            MemoryEvent(
                e["kind"],
                datetime.fromisoformat(str(e["at"])),
                duration,
                None if minimum is None else float(minimum),
                count,
            )
        )
    return MemorySummary(tuple(windows), tuple(events))
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_memory_history.py -q`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/memory_history.py tests/test_memory_history.py
git commit -m "feat: format memory history for both surfaces and the verdict file

Assisted-by: Claude Opus 5.5 (Claude Code)"
```

---

### Task 5: Carry the summary in the snapshot and the verdict file

**Files:**
- Modify: `src/harbor_console/snapshot.py` (field after `gpu_averages`)
- Modify: `src/harbor_console/verdict.py` (`Verdict`, `dumps`, `loads`)
- Test: `tests/test_verdict.py` (append)

**Interfaces:**
- Consumes: `MemorySummary`, `summary_to_json`, `summary_from_json` from Task 4.
- Produces: `Snapshot.memory_summary: MemorySummary | None = None`; `Verdict.memory: MemorySummary | None = None`; the verdict JSON gains `"memory": {...} | null`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_verdict.py`; reuse its existing `VERDICT` constant, and import `replace` from `dataclasses`, `timedelta`, `WINDOWS` from `harbor_console.gpu_history` (the file may already import it), and from `harbor_console.memory_history` import `EVENT_OOM, MemoryEvent, MemorySummary, MemoryWindow`. If `VERDICT.written` is timezone-aware, the event's `at` must be naive: use `VERDICT.written.replace(tzinfo=None)`.)

```python
# Every window, in WINDOWS order: summary_from_json rejects anything else.
WITH_MEMORY = replace(
    VERDICT,
    memory=MemorySummary(
        tuple(
            MemoryWindow(name, seconds, 6.2, 1, 3.5, 1.25, 0, seconds)
            for name, seconds in WINDOWS
        ),
        (MemoryEvent(EVENT_OOM, VERDICT.written - timedelta(minutes=5), 0, None, 1),),
    ),
)


def test_round_trip_with_memory():
    assert loads(dumps(WITH_MEMORY)) == WITH_MEMORY


def test_no_memory_is_written_as_null_and_read_back_as_none():
    payload = json.loads(dumps(VERDICT))
    assert payload["memory"] is None
    assert loads(dumps(VERDICT)).memory is None


def test_a_verdict_without_the_memory_key_still_loads():
    payload = json.loads(dumps(VERDICT))
    del payload["memory"]
    assert loads(json.dumps(payload)) == VERDICT


def test_malformed_memory_makes_the_whole_verdict_none():
    payload = json.loads(dumps(WITH_MEMORY))
    payload["memory"]["windows"][0]["low_episodes"] = "many"
    assert loads(json.dumps(payload)) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_verdict.py -q`
Expected: FAIL, `TypeError: ... unexpected keyword argument 'memory'`.

- [ ] **Step 3: Implement**

`snapshot.py` -- add the import `from harbor_console.memory_history import MemorySummary` and, after `gpu_averages`:

```python
    #: The memory history the prober keeps (ADR 23): low episodes, stall
    #: time and OOM kills per window, and the recent events. None until the
    #: first cycle has recorded a sample.
    memory_summary: MemorySummary | None = None
```

`verdict.py`:
- import `from harbor_console.memory_history import MemorySummary, summary_from_json, summary_to_json`
- `Verdict` gains, after `gpus`: `memory: MemorySummary | None = None`, and its docstring gains: "and the memory history summary (ADR 23), which the console turns into one line."
- in `dumps`, after the `"gpus"` entry: `"memory": None if verdict.memory is None else summary_to_json(verdict.memory),`
- in `loads`, inside the `try`, after `gpus = ...`:

```python
        raw_memory = payload.get("memory")
        memory = None if raw_memory is None else summary_from_json(raw_memory)
```

  and return `Verdict(written=written, hostname=hostname, checks=checks, gpus=gpus, memory=memory)`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_verdict.py tests/test_check.py -q`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/snapshot.py src/harbor_console/verdict.py tests/test_verdict.py
git commit -m "feat: carry the memory summary in the snapshot and the verdict file

Assisted-by: Claude Opus 5.5 (Claude Code)"
```

---

### Task 6: The prober keeps the memory history

**Files:**
- Modify: `src/harbor_console/webapp.py` (`GpuHistoryKeeper` → generic `HistoryFile` + two subclasses; `collect_snapshot`; `VerdictPublisher`; `_default_prober`)
- Test: `tests/test_webapp.py` (append)

**Interfaces:**
- Consumes: `collect_memory_reading`, `MemoryReading` (Task 1); `memory_history` module (Tasks 2–4); `Snapshot.memory_summary`, `Verdict.memory` (Task 5).
- Produces: `HistoryFile[H, I, S](path, reader, writer, record, summarize, report=_report)`, callable `(item: I, now: datetime) -> S`; `GpuHistoryKeeper(path=gpu HISTORY_PATH, reader, writer, report)` (unchanged signature); `MemoryHistoryKeeper(path=memory_history.HISTORY_PATH, reader=memory_history.read_history, writer=memory_history.write_history, report=_report)`; `MemoryKeeper = Callable[[MemoryReading, datetime], MemorySummary | None]`; `no_memory_history(reading, now) -> None`; `collect_snapshot(..., memory: Callable[[], MemoryReading] = collect_memory_reading, memory_keeper: MemoryKeeper = no_memory_history)`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_webapp.py`; the file already has `NOW` and a `collect(**overrides)` helper that calls `collect_snapshot` with fakes -- read lines 600–750 first and reuse them. If `collect` does not pass `memory`, these tests pass it explicitly so no real `/proc` is read.)

```python
from harbor_console import memory_history
from harbor_console.memory_pressure import MemoryReading

LOW_READING = MemoryReading(available_percent=5.0, stall_some_us=0, stall_full_us=0, oom_kills=0, boot=1)


def test_collect_snapshot_passes_the_reading_and_now_to_the_memory_keeper():
    seen = []

    def keeper(reading, now):
        seen.append((reading, now))
        return memory_history.summarize((), now)

    snapshot = collect(memory=lambda: LOW_READING, memory_keeper=keeper)
    assert seen == [(LOW_READING, snapshot.collected)]
    assert snapshot.memory_summary == memory_history.summarize((), snapshot.collected)


def test_collect_snapshot_without_a_memory_keeper_has_no_summary():
    assert collect(memory=lambda: LOW_READING).memory_summary is None


def test_memory_keeper_records_writes_and_summarizes(tmp_path):
    path = tmp_path / "memory-history.json"
    keeper = webapp.MemoryHistoryKeeper(path)
    summary = keeper(LOW_READING, NOW)
    assert summary.windows[0].low_episodes == 1
    assert len(memory_history.read_history(path)) == 1


def test_memory_keeper_loads_what_is_on_disk(tmp_path):
    path = tmp_path / "memory-history.json"
    earlier = memory_history.record((), LOW_READING, NOW - timedelta(seconds=30))
    memory_history.write_history(earlier, path)
    keeper = webapp.MemoryHistoryKeeper(path)
    keeper(LOW_READING, NOW)
    assert len(memory_history.read_history(path)) == 2


def test_memory_keeper_reports_a_failed_write_once_and_still_summarizes(tmp_path):
    reported = []

    def writer(_history, _path):
        raise OSError("disk full")

    keeper = webapp.MemoryHistoryKeeper(tmp_path / "memory-history.json", writer=writer, report=reported.append)
    first = keeper(LOW_READING, NOW)
    second = keeper(LOW_READING, NOW + timedelta(seconds=30))
    assert len(reported) == 1
    assert first.windows[0].low_episodes == 1
    assert second.windows[0].covered_seconds == 30


def test_memory_keeper_defaults_to_the_state_directory():
    assert (
        inspect.signature(webapp.MemoryHistoryKeeper.__init__).parameters["path"].default
        is memory_history.HISTORY_PATH
    )


def test_verdict_publisher_carries_the_memory_summary(tmp_path):
    written = []
    summary = memory_history.summarize((), NOW)
    publisher = webapp.VerdictPublisher(tmp_path / "checks.json", writer=lambda v, _p: written.append(v))
    publisher(Snapshot(collected=NOW, metrics={"hostname": "h"}, memory_summary=summary))
    assert written[0].memory == summary
```

(Add `timedelta`, `inspect` and `Snapshot` imports if the file does not already have them.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_webapp.py -q`
Expected: FAIL, `TypeError: collect_snapshot() got an unexpected keyword argument 'memory'` and `AttributeError: ... 'MemoryHistoryKeeper'`.

- [ ] **Step 3: Implement**

Imports in `webapp.py`:

```python
from harbor_console import memory_history
from harbor_console.memory_history import MemorySummary
from harbor_console.memory_pressure import MemoryReading, collect_memory_reading
```

Below `no_history`:

```python
#: How the prober turns this cycle's memory reading into a summary: the
#: keeper in production, a fake in tests, nothing before either is wired.
MemoryKeeper = Callable[[MemoryReading, datetime], MemorySummary | None]


def no_memory_history(reading: MemoryReading, now: datetime) -> MemorySummary | None:
    """The default: no history kept, nothing to show."""
    return None
```

`collect_snapshot` gains two parameters after `history`:

```python
    memory: Callable[[], MemoryReading] = collect_memory_reading,
    memory_keeper: MemoryKeeper = no_memory_history,
```

and in the `Snapshot(...)` it returns, after `gpu_averages=history(cards, now),`:

```python
        memory_summary=memory_keeper(memory(), now),
```

`VerdictPublisher.__call__` adds `memory=snapshot.memory_summary,` to the `Verdict(...)`.

Replace the whole `GpuHistoryKeeper` class with:

```python
class HistoryFile[H, I, S]:
    """One persistent history and its one writer (ADR 22, ADR 23).

    Loads the file once, then each cycle records this cycle's input,
    rewrites the file and returns the summary. A write that fails is
    reported once per distinct error, like `VerdictPublisher`, and the
    history still advances in memory: the page keeps its summary until the
    disk recovers, and loses at most what was recorded while it was down.
    Called only from the prober thread, so it needs no lock.
    """

    def __init__(
        self,
        path: Path,
        reader: Callable[[Path], H],
        writer: Callable[[H, Path], None],
        record: Callable[[H, I, datetime], H],
        summarize: Callable[[H, datetime], S],
        report: Callable[[str], None] = _report,
    ) -> None:
        self._path = path
        self._writer = writer
        self._record = record
        self._summarize = summarize
        self._report = report
        self._last_error: str | None = None
        self._history = reader(path)

    def __call__(self, item: I, now: datetime) -> S:
        self._history = self._record(self._history, item, now)
        try:
            self._writer(self._history, self._path)
        except Exception as exc:  # noqa: BLE001 - every failure to write must reach the journal
            message = f"could not write {self._path}: {exc}"
            if message != self._last_error:
                self._report(message)
                self._last_error = message
        else:
            self._last_error = None
        return self._summarize(self._history, now)


class GpuHistoryKeeper(HistoryFile[History, tuple[GpuEntry, ...], tuple[GpuAverages, ...]]):
    """The GPU busy history (ADR 22)."""

    def __init__(
        self,
        path: Path = HISTORY_PATH,
        reader: Callable[[Path], History] = read_history,
        writer: Callable[[History, Path], None] = write_history,
        report: Callable[[str], None] = _report,
    ) -> None:
        super().__init__(path, reader, writer, record, averages, report)


class MemoryHistoryKeeper(
    HistoryFile[memory_history.History, MemoryReading, MemorySummary]
):
    """The memory history (ADR 23)."""

    def __init__(
        self,
        path: Path = memory_history.HISTORY_PATH,
        reader: Callable[[Path], memory_history.History] = memory_history.read_history,
        writer: Callable[[memory_history.History, Path], None] = memory_history.write_history,
        report: Callable[[str], None] = _report,
    ) -> None:
        super().__init__(path, reader, writer, memory_history.record, memory_history.summarize, report)
```

`_default_prober`: construct `memory_keeper = MemoryHistoryKeeper()` beside `keeper`, and pass `memory_keeper=memory_keeper` to `collect_snapshot`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_webapp.py -q`
Expected: all passed, including the existing `GpuHistoryKeeper` tests unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/webapp.py tests/test_webapp.py
git commit -m "feat: keep the memory history in the web prober beside the GPU history

Assisted-by: Claude Opus 5.5 (Claude Code)"
```

---

### Task 7: The console line

**Files:**
- Modify: `src/harbor_console/ui.py` (`build_dashboard`)
- Modify: `src/harbor_console/app.py` (`DashboardBuilder` type, `frame()`)
- Test: `tests/test_ui.py`, `tests/test_app.py` (append)

**Interfaces:**
- Consumes: `CONSOLE_LABEL`, `format_console_line`, `MemorySummary` (Task 4); `gpu_history.NOT_REPORTED`; `Verdict.memory` (Task 5).
- Produces: `build_dashboard(metrics, storage=(), gpus=(), banner=None, gpu_averages=(), memory_summary: MemorySummary | None = None, now: datetime | None = None)`; `app.run` calls `renderer(metrics, storage, gpus, banner, averages, memory, now)`.

- [ ] **Step 1: Write the failing tests**

Read `tests/test_ui.py` first for its `METRICS` fixture and how it renders a dashboard to text (reuse that helper; below it is called `render_text`). Append:

```python
from datetime import datetime, timedelta

from harbor_console.memory_history import EVENT_OOM, MemoryEvent, MemorySummary, MemoryWindow

UI_NOW = datetime(2026, 10, 13, 12, 0)
WEEK_SUMMARY = MemorySummary(
    (MemoryWindow("7d", 7 * 86400, 6.2, 3, 30.0, 12.0, 1, 7 * 86400),),
    (MemoryEvent(EVENT_OOM, UI_NOW - timedelta(hours=2), 0, None, 1),),
)


def test_dashboard_shows_the_memory_events_line_under_swap():
    text = render_text(build_dashboard(METRICS, memory_summary=WEEK_SUMMARY, now=UI_NOW))
    lines = text.splitlines()
    swap = next(i for i, line in enumerate(lines) if "Swap" in line)
    assert "Mem events" in lines[swap + 1]
    assert "7d: 3 low · last 2h ago · stall 12s · oom 1" in lines[swap + 1]


def test_dashboard_without_a_summary_says_not_reported():
    text = render_text(build_dashboard(METRICS))
    line = next(line for line in text.splitlines() if "Mem events" in line)
    assert "not reported" in line
```

In `tests/test_app.py`, read how existing tests capture renderer arguments (a fake `renderer` collecting its args) and the fake `verdict_reader`; append a test that a verdict with `memory=WEEK_SUMMARY` reaches the renderer as its sixth argument and the clock's `now` as its seventh, and that a `None` verdict passes `None` as the sixth. Update every existing fake renderer in `tests/test_app.py` that takes exactly five positional arguments to accept `*_rest` so the extra arguments do not break it.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_ui.py tests/test_app.py -q`
Expected: FAIL, unexpected keyword `memory_summary`.

- [ ] **Step 3: Implement**

`ui.py` imports: `from harbor_console.memory_history import CONSOLE_LABEL, MemorySummary, format_console_line`. `build_dashboard` signature gains `memory_summary: MemorySummary | None = None, now: datetime | None = None` after `gpu_averages`; docstring adds "and the memory events line from the status page's history (ADR 23)". After the `Swap` row:

```python
    table.add_row(
        CONSOLE_LABEL,
        NOT_REPORTED if memory_summary is None else format_console_line(memory_summary, now),
    )
```

`app.py`: extend the `DashboardBuilder` `Callable[[...], object]` argument list with `MemorySummary | None, datetime` (import `MemorySummary`). In `frame()`:

```python
        averages = verdict.gpus if verdict is not None else ()
        memory = verdict.memory if verdict is not None else None
        return renderer(collector(), storage_collector(), gpu_collector(), banner, averages, memory, now)
```

Docstring of `run`: append "The memory events line rides in the same read (ADR 23)."

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest -q`
Expected: all passed. If a ui test pinning the dashboard's height or row list fails, update its expectation to include the `Mem events` row and note it in the commit message.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/ui.py src/harbor_console/app.py tests/test_ui.py tests/test_app.py
git commit -m "feat: show the week's memory events on one console line under swap

Assisted-by: Claude Opus 5.5 (Claude Code)"
```

---

### Task 8: The status page section

**Files:**
- Modify: `src/harbor_console/web.py` (new `_memory_section`, placed in the resource grid after the GPU section)
- Test: `tests/test_web.py` (append)

**Interfaces:**
- Consumes: `WINDOW_HEADERS`, `NO_EVENTS`, `window_cells`, `format_event`, `summarize` (Tasks 3–4); `Snapshot.memory_summary` (Task 5).

- [ ] **Step 1: Write the failing tests** (append; read `tests/test_web.py` for its snapshot fixture -- below called `probed_snapshot()` -- and reuse it with `dataclasses.replace`)

```python
from dataclasses import replace
from datetime import timedelta

from harbor_console.memory_history import EVENT_LOW, MemoryEvent, MemorySummary, MemoryWindow


def page(snapshot) -> str:
    return render_page(snapshot).decode("utf-8")


def test_memory_section_before_the_first_record():
    assert "Not reported yet." in page(replace(probed_snapshot(), memory_summary=None))


def test_memory_section_table_and_events():
    snap = probed_snapshot()
    summary = MemorySummary(
        (MemoryWindow("1h", 3600, 6.2, 1, 3.0, 1.0, 0, 3600),),
        (MemoryEvent(EVENT_LOW, snap.collected - timedelta(minutes=30), 720, 6.2, 1),),
    )
    html = page(replace(snap, memory_summary=summary))
    assert "<h2>Memory events</h2>" in html
    assert "<th>min avail</th>" in html
    assert "<td>6.2%</td>" in html
    assert "low 12m, min 6.2%" in html


def test_memory_section_with_no_events_says_so():
    summary = MemorySummary((MemoryWindow("1h", 3600, 50.0, 0, 0.0, 0.0, 0, 3600),), ())
    assert "No low-memory events this week." in page(replace(probed_snapshot(), memory_summary=summary))
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_web.py -q`
Expected: FAIL on the three new tests.

- [ ] **Step 3: Implement**

Import in `web.py`: `from harbor_console.memory_history import NO_EVENTS, WINDOW_HEADERS, format_event, window_cells`. In `render_page`'s resource grid, after the GPU div:

```python
        f'<div class="memory-section">{_memory_section(snapshot)}</div>'
```

Below `_gpu_section`:

```python
def _memory_section(snapshot: Snapshot) -> str:
    """Low episodes, stall time and OOM kills per window, and the week's
    most recent events (ADR 23). `Not reported yet.` before the prober has
    recorded a sample: absence of evidence is not an empty week."""
    if snapshot.memory_summary is None:
        return "<h2>Memory events</h2><p>Not reported yet.</p>"
    summary = snapshot.memory_summary
    head = "".join(f"<th>{escape(h)}</th>" for h in WINDOW_HEADERS)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in window_cells(w)) + "</tr>"
        for w in summary.windows
    )
    if summary.events:
        events = "<ul>" + "".join(f"<li>{escape(format_event(e))}</li>" for e in summary.events) + "</ul>"
    else:
        events = f"<p>{escape(NO_EVENTS)}</p>"
    return f"<h2>Memory events</h2><table><tr>{head}</tr>{body}</table>{events}"
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest -q`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/web.py tests/test_web.py
git commit -m "feat: show memory events per window and the week's recent events on the status page

Assisted-by: Claude Opus 5.5 (Claude Code)"
```

---

### Task 9: ADR 23, deploy wording, CLAUDE.md

**Files:**
- Create: `docs/adr/0023-keep-a-week-of-memory-pressure-history.md` (copy the structure of `docs/adr/template.md`)
- Modify: `docs/adr/README.md` (index line, if it lists ADRs)
- Modify: `deploy/harbor-console-web.service` (comment above `StateDirectory=` names both histories)
- Modify: `deploy/uninstall.sh:78` (message: `(GPU and memory history)`)
- Modify: `CLAUDE.md`
- Test: `tests/test_deploy.py` (only if it asserts the old wording -- run it)

- [ ] **Step 1: Write ADR 23**

```markdown
# 23. Keep a week of memory pressure history beside the GPU history

Date: 2026-10-13

## Status

Accepted

## Context

The Memory row shows the instant: used, total, percent. The question it
cannot answer is whether the host ran short of memory while nobody was
looking -- and when it does run short, it is usually for seconds, ending in
the kernel killing something. A 30 s sample of available memory is the
wrong instrument for that on its own: a burst that comes and goes between
two samples is invisible to it.

The kernel keeps two counters that are the right instrument. PSI's
`/proc/pressure/memory` `total=` is the cumulative time tasks spent stalled
on memory, and `/proc/vmstat` `oom_kill` the cumulative OOM kills, both
since boot. The difference between two readings counts everything between
them, however brief.

ADR 22 made the GPU busy history the project's one persistent file and
said a second needs its own ADR.

## Decision

We will keep a week of memory samples in
`/var/lib/harbor-console/memory-history.json`, beside the GPU history, with
the same single writer (the web prober), the same atomic rewrite, the same
windows and the same 95% coverage rule.

Each sample carries available percent, the PSI `some` and `full` totals,
the OOM kill counter, and `btime`. From them the prober derives, per
window: the minimum available percent; low episodes, runs of samples under
10% available no more than 120 s apart; stall seconds and OOM kills from
counter deltas, where a counter that went down or a boot time that moved
more than 60 s is a reboot. It keeps the five most recent events.

Raw samples are stored, not events, so these rules can change without
losing the week. The 10% line is a constant; there is no configuration.

The page shows a table per window and the recent events. The console shows
one line under Swap from the verdict file it already reads:
`7d: 3 low · last 2h ago · stall 12s · oom 1`. No colour and no banner: an
OOM kill is a workload event, not a platform fault (ADR 21).

## Consequences

- The project keeps two files across restarts. A third needs a new ADR.
- The verdict file carries checks, GPU averages and the memory summary.
- About 20k samples a week, under 1 MB, rewritten every cycle: about 3 GB
  a day, negligible on an SSD, accepted for the same reason as ADR 22.
- The console spends one more of ADR 20's three spare rows. With one GPU
  on hpz440 that leaves one; while the banner is up the panel's bottom is
  cropped further, and ADR 20's remedy (a smaller font) still applies.
- A kernel without PSI shows `—` for stall time and still reports episodes
  and kills. Unknown is never zero.
- Sampling still misses a dip under 10% that recovers between samples; the
  stall and kill counters are what catch those, which is why both exist.
- Rejected: an event-only log (fixes the rules at write time and loses the
  minimum trend); one combined history file (couples two codecs and would
  supersede ADR 22 rather than extend it).
```

- [ ] **Step 2: Update deploy wording**

`deploy/harbor-console-web.service`: in the comment above `StateDirectory=` (line ~35), change "the GPU busy history" to "the GPU busy history and the memory history (ADR 22, ADR 23)", keeping the rest of the comment.
`deploy/uninstall.sh:78`: `echo "==> Removing /var/lib/harbor-console (GPU and memory history)"`.

- [ ] **Step 3: Update `CLAUDE.md`**

- In the dashboard bullet under "What this is", after the GPU averages clause: ", a memory-events line (low episodes, stall time and OOM kills over the week)".
- In the Architecture list, after the `gpu_history.py` bullet, add:
  - `memory_pressure.py` — **collects** available memory from `/proc/meminfo`, the PSI `some`/`full` stall totals from `/proc/pressure/memory`, `oom_kill` from `/proc/vmstat` and `btime` from `/proc/stat`. Files only, each field optional and guarded on its own, never raises. Shared like `gpu.py`.
  - `memory_history.py` — a policy module and codec like `gpu_history.py`, for the second persistent file, `/var/lib/harbor-console/memory-history.json` ([ADR 23](docs/adr/0023-keep-a-week-of-memory-pressure-history.md)): a week of raw samples, summarized per window into minimum available, low episodes (under 10% available, runs broken by a gap over 120 s), stall seconds and OOM kills from counter deltas (a counter that went down, or a `btime` that moved more than 60 s, is a reboot), plus the five newest events. Raw samples, not events, so the rules can change without losing the week.
- In "Five behaviours", the last bullet: "the web prober is the only writer of the GPU history" → "the web prober is the only writer of the GPU and memory histories", and append "The memory summary rides in the same file (ADR 23)."
- In "Scope discipline": "no persistence but the GPU history file ([ADR 22](...))" → "no persistence but the GPU and memory history files ([ADR 22](docs/adr/0022-keep-a-week-of-gpu-busy-history.md), [ADR 23](docs/adr/0023-keep-a-week-of-memory-pressure-history.md))".
- "The web surface is fourteen modules" → "sixteen modules" only if the two new modules are listed under the web surface; they are listed with the shared collectors instead, so leave that count alone.

If `docs/adr/README.md` has an index, add `- [23. Keep a week of memory pressure history beside the GPU history](0023-keep-a-week-of-memory-pressure-history.md)` in order.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all passed. If `tests/test_deploy.py` asserted the old uninstall message, update the assertion to the new text.

- [ ] **Step 5: Commit**

```bash
git add docs/adr/0023-keep-a-week-of-memory-pressure-history.md docs/adr/README.md deploy/harbor-console-web.service deploy/uninstall.sh CLAUDE.md tests/test_deploy.py
git commit -m "docs: ADR 23, keep a week of memory pressure history beside the GPU history

Assisted-by: Claude Opus 5.5 (Claude Code)"
```
