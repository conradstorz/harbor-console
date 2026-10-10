"""What GPUs this host has, from sysfs.

Collects only. One entry per `/sys/class/drm/card<N>`, every metric optional:
a driver exposes what it exposes, and the entry carries a number where one
exists and nothing where none does, so a renderer never decides which driver
it is looking at. On hpz440 the `radeon` driver exposes a temperature and
nothing else; `amdgpu` adds busy percent and VRAM; the proprietary `nvidia`
driver exposes nothing at all through sysfs, so a card it drives is filled
from NVML instead (`nvml.py`), matched by the PCI bus ID `uevent` names.

Reads files and runs no subprocess, so nothing here can hang and nothing
needs a timeout.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from harbor_console.nvml import NvmlMetrics, nvml_metrics

NOTE_UNAVAILABLE = "unavailable"

#: `card0` is a card; `card0-DP-1` is one of its connectors, `renderD128` a
#: render node, `version` a file. Only the first is a GPU.
_CARD = re.compile(r"^card(\d+)$")

#: The one driver whose metrics live behind NVML rather than in sysfs.
_NVIDIA = "nvidia"

#: Marketing prefixes NVML puts before the model; the label has 80 columns to
#: share with the metrics, and "RTX 3060" says what "NVIDIA GeForce RTX 3060"
#: says.
_NAME_PREFIXES = ("NVIDIA ", "GeForce ")

#: hwmon `pwm1` runs 0..`pwm1_max`, and `pwm1_max` is 255 wherever it is
#: absent.
_PWM_MAX_DEFAULT = 255

NvmlQuery = Callable[[str], NvmlMetrics | None]


@dataclass(frozen=True)
class GpuEntry:
    """One GPU, with whatever its driver chose to say about it."""

    label: str
    #: The DRM node (`card1`): the one name for a card that survives NVML
    #: going quiet for a cycle, so it is what history is keyed by. Empty on
    #: the `unavailable` sentinel, which is not a card.
    card: str = ""
    #: The `PCI_SLOT_NAME` from `device/uevent` (`0000:02:00.0`). Empty when
    #: the device is not on PCI or `uevent` is unreadable.
    bus_id: str = ""
    driver: str = ""
    busy_percent: int | None = None
    #: Bytes. Rendered only when both are known and total is non-zero.
    vram_used: int | None = None
    vram_total: int | None = None
    temp_c: float | None = None
    #: Watts. Draw renders alone; a limit renders only against a draw.
    power_w: float | None = None
    power_limit_w: float | None = None
    fan_percent: int | None = None
    note: str = ""


def format_gpu(entry: GpuEntry) -> str:
    """The three shapes an entry renders as, and nothing else.

    Present fields joined in a fixed order; nothing present names the driver
    that stayed silent, so a bare row still says why; a note stands alone.

    Denser than the memory and storage rows on purpose: the console gives a
    value 54 cells (ADR 20), and a full NVIDIA row -- busy, VRAM, heat, power
    against its limit, fan -- has to fit in one line or it eats a row of the
    three the dashboard has to spare. VRAM is `used/total GiB` with no word
    and no percentage; a GPU row has only one memory, and the pair already
    says how full it is.
    """
    if entry.note:
        return entry.note
    parts: list[str] = []
    if entry.busy_percent is not None:
        parts.append(f"busy {entry.busy_percent}%")
    if entry.vram_used is not None and entry.vram_total:
        parts.append(f"{_gib(entry.vram_used)}/{_gib(entry.vram_total, whole=True)} GiB")
    if entry.temp_c is not None:
        parts.append(f"{entry.temp_c:.0f}°C")
    if entry.power_w is not None:
        if entry.power_limit_w is not None:
            parts.append(f"{entry.power_w:.0f}/{entry.power_limit_w:.0f} W")
        else:
            parts.append(f"{entry.power_w:.0f} W")
    if entry.fan_percent is not None:
        parts.append(f"fan {entry.fan_percent}%")
    if parts:
        return " · ".join(parts)
    if entry.driver:
        return f"no metrics exposed by {entry.driver}"
    return "no metrics exposed"


def _gib(size: int, whole: bool = False) -> str:
    """Bytes as GiB to one decimal; `whole` drops a `.0`, since a card's
    total is a round number and the row has no cells to spare."""
    text = f"{size / 1024**3:.1f}"
    return text.removesuffix(".0") if whole else text


def _read_int(path: Path) -> int | None:
    """One sysfs number, or `None` for a file that is missing, unreadable or
    not a number. Guarded per file: one bad value costs only itself."""
    try:
        return int(path.read_text(encoding="ascii", errors="replace").strip())
    except (OSError, ValueError):
        return None


def _uevent(device: Path) -> dict[str, str]:
    """`device/uevent` as a mapping, empty when it cannot be read.

    `DRIVER=` names the driver (a regular file where `device/driver` is a
    symlink, so a test tree needs no link) and `PCI_SLOT_NAME=` the bus ID
    NVML is asked about.
    """
    try:
        text = (device / "uevent").read_text(encoding="ascii", errors="replace")
    except OSError:
        return {}
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def _hwmon_order(path: Path) -> tuple[int, str]:
    """`hwmon2` before `hwmon10`: numeric where there is a number, name otherwise."""
    match = re.search(r"(\d+)$", path.name)
    return (int(match.group(1)) if match else -1, path.name)


def _hwmon(device: Path) -> Path | None:
    """The lowest-numbered hwmon directory under the device that reports a
    temperature, else the lowest that reports a fan, else `None`.

    A directory with neither is skipped rather than chosen, so a sensor that
    sits behind an empty one is still found. Guarded on the listing, so a
    device with no `hwmon` directory simply has no sensors.
    """
    try:
        sensors = sorted((device / "hwmon").iterdir(), key=_hwmon_order)
    except OSError:
        return None
    for probe in ("temp1_input", "pwm1"):
        for sensor in sensors:
            if _read_int(sensor / probe) is not None:
                return sensor
    return None


def _temperature(sensor: Path | None) -> float | None:
    """`temp1_input` in degrees Celsius; sysfs reports millidegrees."""
    if sensor is None:
        return None
    millidegrees = _read_int(sensor / "temp1_input")
    return None if millidegrees is None else millidegrees / 1000.0


def _fan_percent(sensor: Path | None) -> int | None:
    """`pwm1` as a percentage of `pwm1_max`: the duty the driver commands,
    which is what a card without a tachometer can say about its fan."""
    if sensor is None:
        return None
    pwm = _read_int(sensor / "pwm1")
    if pwm is None:
        return None
    pwm_max = _read_int(sensor / "pwm1_max") or _PWM_MAX_DEFAULT
    return round(100 * pwm / pwm_max)


def _short_name(name: str) -> str:
    for prefix in _NAME_PREFIXES:
        name = name.removeprefix(prefix)
    return name


def _card_entry(node: Path, nvml: NvmlQuery) -> GpuEntry:
    device = node / "device"
    uevent = _uevent(device)
    driver = uevent.get("DRIVER", "")
    sensor = _hwmon(device)
    bus_id = uevent.get("PCI_SLOT_NAME", "")
    entry = GpuEntry(
        label=_label(node.name, driver),
        card=node.name,
        bus_id=bus_id,
        driver=driver,
        busy_percent=_read_int(device / "gpu_busy_percent"),
        vram_used=_read_int(device / "mem_info_vram_used"),
        vram_total=_read_int(device / "mem_info_vram_total"),
        temp_c=_temperature(sensor),
        fan_percent=_fan_percent(sensor),
    )
    if driver != _NVIDIA or not bus_id:
        return entry
    metrics = nvml(bus_id)
    if metrics is None:
        return entry
    fields = {k: v for k, v in vars(metrics).items() if k != "name"}
    return replace(entry, label=_label(node.name, _short_name(metrics.name) or driver), **fields)


def _label(card: str, detail: str) -> str:
    """`GPU card1 (RTX 3060)`: the model where NVML names one, the driver
    otherwise, bare when even that is unknown."""
    return f"GPU {card} ({detail})" if detail else f"GPU {card}"


def collect_gpus(
    drm_root: str | Path = "/sys/class/drm", nvml: NvmlQuery = nvml_metrics
) -> tuple[GpuEntry, ...]:
    """One entry per card under `drm_root`, in card-number order.

    A card driven by `nvidia` is filled from `nvml` by its PCI bus ID; sysfs
    holds nothing for it, and `nvml` answering `None` leaves the row bare,
    naming the driver that stayed silent.

    A root that cannot be listed yields one `unavailable` entry rather than an
    empty tuple -- nothing found and nothing looked at must not render alike
    (ADR 18). A root with no cards yields an empty tuple; the renderers own
    the "none detected" wording.
    """
    root = Path(drm_root)
    try:
        names = [p.name for p in root.iterdir()]
    except OSError:
        return (GpuEntry(label="GPU", note=NOTE_UNAVAILABLE),)

    cards: list[tuple[int, str]] = []
    for name in names:
        match = _CARD.match(name)
        if match:
            cards.append((int(match.group(1)), name))
    cards.sort()
    return tuple(_card_entry(root / name, nvml) for _, name in cards)
