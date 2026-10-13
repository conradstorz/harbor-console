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

from harbor_console.gpu_history import FULL_COVERAGE, NO_MEAN, WINDOWS
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
