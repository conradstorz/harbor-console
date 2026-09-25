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
