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
    return min(100.0, round(available * 100 / total, 1))


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
