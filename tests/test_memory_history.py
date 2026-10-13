"""A week of memory samples: record, prune, codec."""

from datetime import datetime, timedelta

from harbor_console.memory_history import (
    RETENTION,
    Sample,
    dumps,
    loads,
    read_history,
    record,
    write_history,
)
from harbor_console.memory_pressure import MemoryReading

NOW = datetime(2026, 10, 13, 12, 0, 0)
READING = MemoryReading(
    available_percent=42.5, stall_some_us=10, stall_full_us=5, oom_kills=0, boot=1760000000
)


def sample(at: datetime, available: float | None = 50.0, some=0, full=0, oom=0, boot=1760000000) -> Sample:
    return Sample(at, available, some, full, oom, boot)


def test_record_appends_one_sample_at_now():
    assert record((), READING, NOW) == (Sample(NOW, 42.5, 10, 5, 0, 1760000000),)


def test_record_keeps_a_sample_that_read_nothing():
    assert record((), MemoryReading(), NOW) == (Sample(NOW, None, None, None, None, None),)


def test_record_prunes_samples_older_than_retention():
    old = sample(NOW - RETENTION - timedelta(seconds=1))
    edge = sample(NOW - RETENTION)
    assert record((old, edge), READING, NOW)[:1] == (edge,)
    assert len(record((old, edge), READING, NOW)) == 2


def test_record_never_mutates_its_input():
    history = (sample(NOW - timedelta(minutes=1)),)
    record(history, READING, NOW)
    assert history == (sample(NOW - timedelta(minutes=1)),)


def test_codec_round_trip():
    history = (
        sample(NOW - timedelta(seconds=30), available=7.3, some=100, full=50, oom=1),
        Sample(NOW, None, None, None, None, None),
    )
    assert loads(dumps(history)) == history


def test_dumps_is_one_compact_line():
    text = dumps((sample(NOW),))
    assert "\n" not in text
    assert " " not in text


def test_loads_garbage_is_empty():
    for text in [
        "",
        "not json",
        "[]",
        '{"samples": {}}',
        '{"samples": [[1, 2]]}',
        '{"samples": [["x", 1, 1, 1, 1, 1]]}',
        '{"samples": [[1, 101, 1, 1, 1, 1]]}',
        '{"samples": [[1, 5, -1, 1, 1, 1]]}',
        '{"samples": [[1, true, 1, 1, 1, 1]]}',
        '{"samples": [[1, 5, 1.5, 1, 1, 1]]}',
    ]:
        assert loads(text) == (), text


def test_read_history_of_a_missing_file_is_empty(tmp_path):
    assert read_history(tmp_path / "missing.json") == ()


def test_write_then_read_leaves_no_temp_file(tmp_path):
    path = tmp_path / "memory-history.json"
    history = (sample(NOW),)
    write_history(history, path)
    assert read_history(path) == history
    assert [p.name for p in tmp_path.iterdir()] == ["memory-history.json"]
