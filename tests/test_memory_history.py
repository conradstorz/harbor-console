"""A week of memory samples: record, prune, codec."""

from datetime import datetime, timedelta

from harbor_console.memory_history import (
    EVENT_LOW,
    EVENT_OOM,
    RETENTION,
    MemoryEvent,
    Sample,
    dumps,
    loads,
    read_history,
    record,
    summarize,
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


def at(seconds_ago: int) -> datetime:
    return NOW - timedelta(seconds=seconds_ago)


def window(summary, name):
    return next(w for w in summary.windows if w.window == name)


def test_empty_history_has_every_window_unknown():
    summary = summarize((), NOW)
    assert [w.window for w in summary.windows] == ["1h", "3h", "7h", "24h", "7d"]
    for w in summary.windows:
        assert (w.min_available, w.low_episodes, w.stall_some_seconds, w.stall_full_seconds, w.oom_kills, w.covered_seconds) == (
            None, 0, None, None, None, 0,
        )
    assert summary.events == ()


def test_min_available_and_coverage():
    history = (sample(at(7200), available=30.0), sample(at(60), available=12.5), sample(at(0), available=20.0))
    summary = summarize(history, NOW)
    assert window(summary, "1h").min_available == 12.5
    assert window(summary, "1h").covered_seconds == 60
    assert window(summary, "3h").min_available == 12.5
    assert window(summary, "3h").covered_seconds == 7200


def test_consecutive_low_samples_are_one_episode():
    history = (
        sample(at(120), available=50.0),
        sample(at(90), available=9.0),
        sample(at(60), available=6.2),
        sample(at(30), available=8.0),
        sample(at(0), available=50.0),
    )
    summary = summarize(history, NOW)
    assert window(summary, "1h").low_episodes == 1
    assert summary.events == (MemoryEvent(EVENT_LOW, at(90), 60, 6.2, 1),)


def test_a_recovered_sample_splits_episodes():
    history = (sample(at(90), available=5.0), sample(at(60), available=50.0), sample(at(30), available=5.0))
    assert window(summarize(history, NOW), "1h").low_episodes == 2


def test_an_unread_sample_splits_episodes():
    history = (sample(at(90), available=5.0), sample(at(60), available=None), sample(at(30), available=5.0))
    assert window(summarize(history, NOW), "1h").low_episodes == 2


def test_a_gap_over_the_limit_splits_episodes():
    history = (sample(at(300), available=5.0), sample(at(150), available=5.0))
    assert window(summarize(history, NOW), "1h").low_episodes == 2


def test_exactly_ten_percent_is_not_low():
    assert window(summarize((sample(at(0), available=10.0),), NOW), "1h").low_episodes == 0


def test_an_episode_counts_in_a_window_when_any_sample_is_inside():
    history = (sample(at(3660), available=5.0), sample(at(3600), available=5.0))
    assert window(summarize(history, NOW), "1h").low_episodes == 1


def test_counter_deltas_sum_into_windows():
    history = (
        sample(at(7200), some=1_000_000, full=0, oom=0),
        sample(at(60), some=3_000_000, full=500_000, oom=1),
        sample(at(0), some=3_500_000, full=500_000, oom=3),
    )
    summary = summarize(history, NOW)
    one_hour = window(summary, "1h")
    assert one_hour.stall_some_seconds == 2.5
    assert one_hour.stall_full_seconds == 0.5
    assert one_hour.oom_kills == 3


def test_a_steady_counter_is_zero_not_unknown():
    history = (sample(at(30), oom=4), sample(at(0), oom=4))
    assert window(summarize(history, NOW), "1h").oom_kills == 0


def test_a_counter_that_went_down_is_a_reboot():
    history = (sample(at(30), oom=9, boot=1), sample(at(0), oom=2, boot=1))
    assert window(summarize(history, NOW), "1h").oom_kills == 2


def test_a_changed_boot_is_a_reboot():
    history = (sample(at(30), oom=1, boot=1760000000), sample(at(0), oom=3, boot=1760090000))
    assert window(summarize(history, NOW), "1h").oom_kills == 3


def test_boot_jitter_within_tolerance_is_the_same_boot():
    history = (sample(at(30), oom=1, boot=1760000000), sample(at(0), oom=3, boot=1760000002))
    assert window(summarize(history, NOW), "1h").oom_kills == 2


def test_a_window_with_no_usable_pair_is_unknown():
    history = (sample(at(30), oom=None), sample(at(0), oom=4))
    assert window(summarize(history, NOW), "1h").oom_kills is None


def test_an_oom_delta_is_an_event_at_the_later_sample():
    history = (sample(at(30), oom=0), sample(at(0), oom=2))
    assert summarize(history, NOW).events == (MemoryEvent(EVENT_OOM, at(0), 0, None, 2),)


def test_events_are_newest_first_and_capped():
    history = tuple(sample(at(600 - 60 * i), available=5.0 if i % 2 == 0 else 50.0) for i in range(11))
    events = summarize(history, NOW).events
    assert len(events) == 5
    assert [e.at for e in events] == sorted((e.at for e in events), reverse=True)
    assert events[0].at == at(0)
