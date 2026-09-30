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

    def __init__(
        self,
        bus_ids=("0000:02:00.0",),
        busy=12,
        used=5,
        total=12,
        temp=48,
        name=b"NVIDIA GeForce RTX 3060",
        power_mw=167380,
        limit_mw=170000,
        fan=88,
        fail=(),
    ):
        self.bus_ids = bus_ids
        self.busy, self.used, self.total, self.temp = busy, used, total, temp
        self.name, self.power_mw, self.limit_mw, self.fan = name, power_mw, limit_mw, fan
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

    def nvmlDeviceGetName(self, handle, buffer, length):
        if "name" in self.fail:
            return FAILURE
        assert length == len(buffer) >= 96  # NVML_DEVICE_NAME_V2_BUFFER_SIZE
        buffer.value = self.name
        return SUCCESS

    def nvmlDeviceGetPowerUsage(self, handle, milliwatts):
        if "power" in self.fail:
            return FAILURE
        milliwatts.contents.value = self.power_mw
        return SUCCESS

    def nvmlDeviceGetPowerManagementLimit(self, handle, milliwatts):
        if "limit" in self.fail:
            return FAILURE
        milliwatts.contents.value = self.limit_mw
        return SUCCESS

    def nvmlDeviceGetFanSpeed(self, handle, percent):
        if "fan" in self.fail:
            return FAILURE
        percent.contents.value = self.fan
        return SUCCESS


FULL = dict(
    name="NVIDIA GeForce RTX 3060",
    busy_percent=12,
    vram_used=5,
    vram_total=12,
    temp_c=48.0,
    power_w=167.38,
    power_limit_w=170.0,
    fan_percent=88,
)


def without(*keys):
    return NvmlMetrics(**{k: v for k, v in FULL.items() if k not in keys})


def test_query_reads_every_metric_for_the_card_at_the_bus_id():
    lib = FakeNvml(busy=12, used=5 * 1024**3, total=12 * 1024**3, temp=48)

    assert query(lib, "0000:02:00.0") == NvmlMetrics(
        name="NVIDIA GeForce RTX 3060",
        busy_percent=12,
        vram_used=5 * 1024**3,
        vram_total=12 * 1024**3,
        temp_c=48.0,
        power_w=167.38,
        power_limit_w=170.0,
        fan_percent=88,
    )
    assert lib.calls == ["0000:02:00.0"]


def test_query_returns_none_for_a_bus_id_nvml_does_not_know():
    assert query(FakeNvml(bus_ids=("0000:05:00.0",)), "0000:02:00.0") is None


def test_query_drops_only_the_metric_whose_call_failed():
    assert query(FakeNvml(fail=("util",)), "0000:02:00.0") == without("busy_percent")
    assert query(FakeNvml(fail=("memory",)), "0000:02:00.0") == without("vram_used", "vram_total")
    assert query(FakeNvml(fail=("temp",)), "0000:02:00.0") == without("temp_c")
    assert query(FakeNvml(fail=("name",)), "0000:02:00.0") == without("name")
    assert query(FakeNvml(fail=("power",)), "0000:02:00.0") == without("power_w")
    assert query(FakeNvml(fail=("limit",)), "0000:02:00.0") == without("power_limit_w")
    assert query(FakeNvml(fail=("fan",)), "0000:02:00.0") == without("fan_percent")


def test_query_decodes_the_name_and_ignores_a_name_that_is_not_ascii():
    assert query(FakeNvml(name=b"Quadro P400"), "0000:02:00.0").name == "Quadro P400"
    assert query(FakeNvml(name=b"\xff\xfe"), "0000:02:00.0").name == ""


def test_query_survives_a_library_missing_a_function(monkeypatch):
    monkeypatch.delattr(FakeNvml, "nvmlDeviceGetTemperature")

    assert query(FakeNvml(), "0000:02:00.0") == without("temp_c")


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

    assert nvml_metrics("0000:02:00.0", library=lambda: lib) == NvmlMetrics(**{**FULL, "temp_c": 40.0})


def test_nvml_metrics_defaults_to_the_cached_real_library():
    assert inspect.signature(nvml_metrics).parameters["library"].default is cached_library
    assert inspect.signature(load_library).parameters["loader"].default is ctypes.CDLL
