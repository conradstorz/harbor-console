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


def test_available_above_total_is_capped_at_one_hundred():
    files = dict(FILES)
    files[MEMINFO] = "MemTotal: 100 kB\nMemAvailable: 150 kB\n"
    assert collect_memory_reading(reader(files)).available_percent == 100.0
