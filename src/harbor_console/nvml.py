"""NVIDIA GPU metrics through NVML, for the card sysfs says nothing about.

Collects only. The proprietary `nvidia` driver publishes no busy percent, no
VRAM and no hwmon under `/sys/class/drm/card<N>/device`; the numbers exist
only behind `libnvidia-ml.so.1`, which is what `nvidia-smi` itself reads. So
this module reaches it directly with `ctypes`: no subprocess, no dependency,
and readable by an unprivileged user because `/dev/nvidia*` is world-readable.

Every failure is a `None`, never an exception: a missing library, a driver
that will not initialise, a bus ID NVML does not know, or one metric call
that errors, each cost only what they cover. `gpu.py` asks here only for a
card whose driver is `nvidia`, so a host without one never loads anything.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

LIBRARY = "libnvidia-ml.so.1"

_SUCCESS = 0
_TEMPERATURE_GPU = 0
#: NVML_DEVICE_NAME_V2_BUFFER_SIZE.
_NAME_BUFFER = 96


class _Utilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _Memory(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


@dataclass(frozen=True)
class NvmlMetrics:
    """What NVML said about one card; each field `None` where it would not."""

    name: str = ""
    busy_percent: int | None = None
    vram_used: int | None = None
    vram_total: int | None = None
    temp_c: float | None = None
    #: Watts; NVML reports milliwatts and both are converted here.
    power_w: float | None = None
    power_limit_w: float | None = None
    fan_percent: int | None = None


def _call(lib: Any, name: str, *args: Any) -> bool:
    """One NVML call, true on `NVML_SUCCESS`. A library without the function
    or a call that raises reads as failure, so a stale or odd build costs
    only the metric it was asked for."""
    try:
        return getattr(lib, name)(*args) == _SUCCESS
    except (AttributeError, ctypes.ArgumentError, OSError):
        return False


def query(lib: Any, bus_id: str) -> NvmlMetrics | None:
    """The metrics of the card at `bus_id` (`0000:02:00.0`, as sysfs spells
    it), from an initialised `lib`; `None` when NVML has no such card."""
    handle = ctypes.c_void_p()
    if not _call(lib, "nvmlDeviceGetHandleByPciBusId_v2", bus_id.encode("ascii"), ctypes.pointer(handle)):
        return None

    metrics = NvmlMetrics()
    name = ctypes.create_string_buffer(_NAME_BUFFER)
    if _call(lib, "nvmlDeviceGetName", handle, name, _NAME_BUFFER):
        try:
            metrics = replace(metrics, name=name.value.decode("ascii").strip())
        except UnicodeDecodeError:
            pass
    util = _Utilization()
    if _call(lib, "nvmlDeviceGetUtilizationRates", handle, ctypes.pointer(util)):
        metrics = replace(metrics, busy_percent=int(util.gpu))
    memory = _Memory()
    if _call(lib, "nvmlDeviceGetMemoryInfo", handle, ctypes.pointer(memory)):
        metrics = replace(metrics, vram_used=int(memory.used), vram_total=int(memory.total))
    temp = ctypes.c_uint()
    if _call(lib, "nvmlDeviceGetTemperature", handle, _TEMPERATURE_GPU, ctypes.pointer(temp)):
        metrics = replace(metrics, temp_c=float(temp.value))
    milliwatts = ctypes.c_uint()
    if _call(lib, "nvmlDeviceGetPowerUsage", handle, ctypes.pointer(milliwatts)):
        metrics = replace(metrics, power_w=milliwatts.value / 1000.0)
    if _call(lib, "nvmlDeviceGetPowerManagementLimit", handle, ctypes.pointer(milliwatts)):
        metrics = replace(metrics, power_limit_w=milliwatts.value / 1000.0)
    fan = ctypes.c_uint()
    if _call(lib, "nvmlDeviceGetFanSpeed", handle, ctypes.pointer(fan)):
        metrics = replace(metrics, fan_percent=int(fan.value))
    return metrics


def load_library(loader: Callable[[str], Any] = ctypes.CDLL) -> Any | None:
    """`LIBRARY` loaded and initialised, or `None` when it is absent or the
    driver refuses `nvmlInit_v2`."""
    try:
        lib = loader(LIBRARY)
    except OSError:
        return None
    if not _call(lib, "nvmlInit_v2"):
        return None
    return lib


_loaded: Any | None = None


def cached_library() -> Any | None:
    """Load and initialise once per process; a failed load is retried next
    time, so a driver that arrives later is not missed."""
    global _loaded
    if _loaded is None:
        _loaded = load_library()
    return _loaded


def nvml_metrics(
    bus_id: str, library: Callable[[], Any | None] = cached_library
) -> NvmlMetrics | None:
    """The `gpu.py` entry point: metrics for the card at `bus_id`, or `None`."""
    lib = library()
    if lib is None:
        return None
    return query(lib, bus_id)
