import ctypes
import inspect

from harbor_console.nvml import (
    LIBRARY,
    NvmlMetrics,
    cached_library,
    load_library,
    nvml_metrics,
    query,
)

SUCCESS = 0
FAILURE = 999


class FakeNvml:
    """Enough of `libnvidia-ml.so.1` to drive `query`: functions that write
    through ctypes pointers and return a status, like the real ones."""

    def __init__(self, bus_ids=("0000:02:00.0",), busy=12, used=5, total=12, temp=48, fail=()):
        self.bus_ids = bus_ids
        self.busy, self.used, self.total, self.temp = busy, used, total, temp
        self.fail = set(fail)
        self.calls: list[str] = []
        self.inits = 0

    def nvmlInit_v2(self):
        self.inits += 1
        return FAILURE if "init" in self.fail else SUCCESS

    def nvmlDeviceGetHandleByPciBusId_v2(self, bus_id, handle):
        self.calls.append(bus_id.decode())
        if bus_id.decode() not in self.bus_ids or "handle" in self.fail:
            return FAILURE
        handle.contents.value = 1234
        return SUCCESS

    def nvmlDeviceGetUtilizationRates(self, handle, util):
        if "util" in self.fail:
            return FAILURE
        util.contents.gpu = self.busy
        return SUCCESS

    def nvmlDeviceGetMemoryInfo(self, handle, memory):
        if "memory" in self.fail:
            return FAILURE
        memory.contents.used = self.used
        memory.contents.total = self.total
        return SUCCESS

    def nvmlDeviceGetTemperature(self, handle, sensor, temp):
        if "temp" in self.fail:
            return FAILURE
        assert sensor == 0  # NVML_TEMPERATURE_GPU
        temp.contents.value = self.temp
        return SUCCESS


def test_query_reads_every_metric_for_the_card_at_the_bus_id():
    lib = FakeNvml(busy=12, used=5 * 1024**3, total=12 * 1024**3, temp=48)

    assert query(lib, "0000:02:00.0") == NvmlMetrics(
        busy_percent=12, vram_used=5 * 1024**3, vram_total=12 * 1024**3, temp_c=48.0
    )
    assert lib.calls == ["0000:02:00.0"]


def test_query_returns_none_for_a_bus_id_nvml_does_not_know():
    assert query(FakeNvml(bus_ids=("0000:05:00.0",)), "0000:02:00.0") is None


def test_query_drops_only_the_metric_whose_call_failed():
    assert query(FakeNvml(fail=("util",)), "0000:02:00.0") == NvmlMetrics(
        vram_used=5, vram_total=12, temp_c=48.0
    )
    assert query(FakeNvml(fail=("memory",)), "0000:02:00.0") == NvmlMetrics(
        busy_percent=12, temp_c=48.0
    )
    assert query(FakeNvml(fail=("temp",)), "0000:02:00.0") == NvmlMetrics(
        busy_percent=12, vram_used=5, vram_total=12
    )


def test_query_survives_a_library_missing_a_function():
    lib = FakeNvml()
    del FakeNvml.nvmlDeviceGetTemperature
    try:
        assert query(lib, "0000:02:00.0") == NvmlMetrics(busy_percent=12, vram_used=5, vram_total=12)
    finally:
        FakeNvml.nvmlDeviceGetTemperature = _restore_temperature


def _restore_temperature(self, handle, sensor, temp):
    temp.contents.value = self.temp
    return SUCCESS


def test_load_library_initialises_nvml_once_and_returns_it():
    lib = FakeNvml()

    assert load_library(loader=lambda name: lib) is lib
    assert lib.inits == 1


def test_load_library_returns_none_when_the_library_is_missing():
    def loader(name):
        raise OSError(f"{name}: cannot open shared object file")

    assert load_library(loader=loader) is None


def test_load_library_returns_none_when_init_fails():
    assert load_library(loader=lambda name: FakeNvml(fail=("init",))) is None


def test_load_library_asks_for_the_versioned_soname():
    names = []

    def loader(name):
        names.append(name)
        raise OSError

    load_library(loader=loader)

    assert names == [LIBRARY] == ["libnvidia-ml.so.1"]


def test_nvml_metrics_returns_none_when_no_library_loads():
    assert nvml_metrics("0000:02:00.0", library=lambda: None) is None


def test_nvml_metrics_queries_the_loaded_library():
    lib = FakeNvml(temp=40)

    assert nvml_metrics("0000:02:00.0", library=lambda: lib) == NvmlMetrics(
        busy_percent=12, vram_used=5, vram_total=12, temp_c=40.0
    )


def test_nvml_metrics_defaults_to_the_cached_real_library():
    assert inspect.signature(nvml_metrics).parameters["library"].default is cached_library
    assert inspect.signature(load_library).parameters["loader"].default is ctypes.CDLL
