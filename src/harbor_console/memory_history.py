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
