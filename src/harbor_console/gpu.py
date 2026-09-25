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
