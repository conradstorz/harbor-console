"""A week of memory samples: record, prune, codec."""

from datetime import datetime, timedelta

import pytest

from harbor_console.memory_history import (
    EVENT_LOW,
    EVENT_OOM,
    RETENTION,
    MemoryEvent,
    MemorySummary,
    MemoryWindow,
    Sample,
    dumps,
    format_console_line,
    format_duration,
    format_event,
    loads,
    read_history,
    record,
    summarize,
    summary_from_json,
    summary_to_json,
    window_cells,
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


WEEK = 7 * 86400


def full_week(**overrides) -> MemoryWindow:
    values = dict(
        window="7d", seconds=WEEK, min_available=6.2, low_episodes=3,
        stall_some_seconds=30.0, stall_full_seconds=12.4, oom_kills=1, covered_seconds=WEEK,
    )
    values.update(overrides)
    return MemoryWindow(**values)


def summary_with(week: MemoryWindow, events=()) -> MemorySummary:
    others = tuple(
        MemoryWindow(name, secs, None, 0, None, None, None, 0)
        for name, secs in [("1h", 3600), ("3h", 10800), ("7h", 25200), ("24h", 86400)]
    )
    return MemorySummary(others + (week,), tuple(events))


def test_format_duration_units():
    assert [format_duration(s) for s in (0, 12.4, 59, 60, 3599, 3600, 86399, 86400, 200000)] == [
        "0s", "12s", "59s", "1m", "59m", "1h", "23h", "1d", "2d",
    ]


def test_console_line_with_an_event():
    event = MemoryEvent(EVENT_LOW, NOW - timedelta(hours=2), 600, 6.2, 1)
    assert format_console_line(summary_with(full_week(), [event]), NOW) == (
        "7d: 3 low · last 2h ago · stall 12s · oom 1"
    )


def test_console_line_without_events_omits_last():
    week = full_week(low_episodes=0, stall_full_seconds=0.0, oom_kills=0)
    assert format_console_line(summary_with(week), NOW) == "7d: 0 low · stall 0s · oom 0"


def test_console_line_without_a_clock_omits_last():
    event = MemoryEvent(EVENT_OOM, NOW, 0, None, 1)
    assert "last" not in format_console_line(summary_with(full_week(), [event]), None)


def test_console_line_unknown_counters_are_dashes():
    week = full_week(stall_full_seconds=None, oom_kills=None)
    assert format_console_line(summary_with(week), NOW) == "7d: 3 low · stall — · oom —"


def test_console_line_flags_a_partly_covered_week():
    week = full_week(covered_seconds=2 * 86400 + 5)
    assert format_console_line(summary_with(week), NOW).startswith("7d (2d): ")


def test_console_line_never_reports_a_negative_age():
    event = MemoryEvent(EVENT_OOM, NOW + timedelta(minutes=5), 0, None, 1)
    assert "last 0s ago" in format_console_line(summary_with(full_week(), [event]), NOW)


def test_fullest_console_line_fits_the_value_column():
    week = full_week(low_episodes=99, stall_full_seconds=3599, oom_kills=99, covered_seconds=6 * 86400 + 1)
    event = MemoryEvent(EVENT_LOW, NOW - timedelta(hours=23, minutes=59), 0, 1.0, 1)
    line = format_console_line(summary_with(week, [event]), NOW)
    assert line == "7d (6d): 99 low · last 23h ago · stall 59m · oom 99"
    assert len(line) <= 54


def test_window_cells():
    assert window_cells(full_week()) == ("7d", "6.2%", "3", "30s", "12s", "1")
    assert window_cells(MemoryWindow("1h", 3600, None, 0, None, None, None, 0)) == (
        "1h", "—", "0", "—", "—", "—",
    )
    assert window_cells(MemoryWindow("1h", 3600, 50.0, 0, 0.0, 0.0, 0, 1200))[0] == "1h (20m)"


def test_format_event():
    low = MemoryEvent(EVENT_LOW, datetime(2026, 10, 13, 14, 2), 720, 6.2, 1)
    oom = MemoryEvent(EVENT_OOM, datetime(2026, 10, 13, 14, 14), 0, None, 1)
    assert format_event(low) == "2026-10-13 14:02 low 12m, min 6.2%"
    assert format_event(oom) == "2026-10-13 14:14 oom ×1"


def test_summary_json_round_trip():
    events = [
        MemoryEvent(EVENT_OOM, NOW, 0, None, 2),
        MemoryEvent(EVENT_LOW, NOW - timedelta(hours=2), 600, 6.2, 1),
    ]
    summary = summary_with(full_week(), events)
    assert summary_from_json(summary_to_json(summary)) == summary


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(windows="x"),
        lambda d: d["windows"][0].update(window="2h"),
        lambda d: d["windows"][0].update(low_episodes=-1),
        lambda d: d["windows"][0].update(min_available=120),
        lambda d: d["windows"][0].update(oom_kills=True),
        lambda d: d["events"].append({"kind": "boom", "at": NOW.isoformat(), "duration_seconds": 0, "min_available": None, "count": 1}),
        lambda d: d.pop("events"),
        lambda d: d.update(windows=[]),
        lambda d: d["windows"].pop(0),
        lambda d: d["windows"].reverse(),
        lambda d: d["windows"][-1].update(stall_full_seconds=float("inf")),
        lambda d: d["windows"][-1].update(stall_some_seconds=float("nan")),
        lambda d: d["windows"][-1].update(min_available=float("nan")),
        lambda d: d["events"][0].update(at=12345),
        lambda d: d["events"][0].update(at="2026-10-13T14:00:00+00:00"),
        lambda d: d["events"].reverse(),
    ],
)
def test_summary_from_json_rejects_malformed(mutate):
    events = [
        MemoryEvent(EVENT_OOM, NOW, 0, None, 2),
        MemoryEvent(EVENT_LOW, NOW - timedelta(hours=2), 600, 6.2, 1),
    ]
    raw = summary_to_json(summary_with(full_week(), events))
    mutate(raw)
    with pytest.raises((ValueError, KeyError, TypeError)):
        summary_from_json(raw)
