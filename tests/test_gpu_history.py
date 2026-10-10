from datetime import datetime, timedelta

from harbor_console.gpu import GpuEntry
from harbor_console.gpu_history import (
    RETENTION,
    WINDOWS,
    GpuAverages,
    Sample,
    WindowAverage,
    averages,
    record,
)

NOW = datetime(2026, 10, 9, 12, 0, 0)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def entry(card: str, busy: int | None) -> GpuEntry:
    return GpuEntry(label=f"GPU {card}", card=card, driver="amdgpu", busy_percent=busy)


# --- record ---------------------------------------------------------------


def test_record_appends_one_sample_per_card_with_a_busy_percent():
    history = record({}, (entry("card0", 12), entry("card1", 99)), NOW)

    assert history == {
        "card0": (Sample(NOW, 12),),
        "card1": (Sample(NOW, 99),),
    }


def test_record_skips_a_card_whose_driver_said_nothing():
    history = record({}, (entry("card0", None),), NOW)

    assert history == {}


def test_record_skips_the_unavailable_sentinel():
    sentinel = GpuEntry(label="GPU", note="unavailable", busy_percent=5)

    assert record({}, (sentinel,), NOW) == {}


def test_record_appends_after_existing_samples_oldest_first():
    earlier = {"card0": (Sample(NOW - HOUR, 10),)}

    history = record(earlier, (entry("card0", 20),), NOW)

    assert history["card0"] == (Sample(NOW - HOUR, 10), Sample(NOW, 20))


def test_record_prunes_samples_older_than_the_retention():
    old = Sample(NOW - RETENTION - timedelta(seconds=1), 1)
    edge = Sample(NOW - RETENTION, 2)
    history = {"card0": (old, edge)}

    assert record(history, (entry("card0", 3),), NOW)["card0"] == (edge, Sample(NOW, 3))


def test_record_drops_a_card_with_nothing_left():
    history = {"card9": (Sample(NOW - RETENTION - DAY, 1),)}

    assert record(history, (), NOW) == {}


def test_record_never_mutates_its_input():
    original = {"card0": (Sample(NOW - HOUR, 10),)}
    snapshot = {card: samples for card, samples in original.items()}

    record(original, (entry("card0", 20),), NOW)

    assert original == snapshot


def test_record_keeps_a_card_that_went_quiet_this_cycle():
    history = {"card0": (Sample(NOW - HOUR, 10),)}

    assert record(history, (entry("card0", None),), NOW) == history


# --- averages -------------------------------------------------------------


def test_windows_are_the_five_the_surfaces_show():
    assert WINDOWS == (
        ("1h", 3600),
        ("3h", 3 * 3600),
        ("7h", 7 * 3600),
        ("24h", 86400),
        ("7d", 7 * 86400),
    )


def test_averages_returns_one_entry_per_card_in_card_order():
    history = {
        "card10": (Sample(NOW, 1),),
        "card2": (Sample(NOW, 2),),
        "card0": (Sample(NOW, 3),),
    }

    assert [a.card for a in averages(history, NOW)] == ["card0", "card2", "card10"]


def test_averages_has_every_window_for_a_card():
    (result,) = averages({"card0": (Sample(NOW, 50),)}, NOW)

    assert [(w.window, w.seconds) for w in result.windows] == list(WINDOWS)


def test_a_window_is_the_plain_rounded_mean_of_the_samples_inside_it():
    samples = (
        Sample(NOW - timedelta(minutes=50), 10),
        Sample(NOW - timedelta(minutes=30), 20),
        Sample(NOW - timedelta(minutes=10), 31),
    )

    (result,) = averages({"card0": samples}, NOW)
    one_hour = result.windows[0]

    assert one_hour == WindowAverage("1h", 3600, 20, 50 * 60)


def test_a_sample_on_the_window_boundary_is_inside_it():
    samples = (Sample(NOW - HOUR, 100), Sample(NOW, 0))

    (result,) = averages({"card0": samples}, NOW)

    assert result.windows[0].mean == 50
    assert result.windows[0].covered_seconds == 3600


def test_a_sample_just_outside_the_window_is_not_counted():
    samples = (Sample(NOW - HOUR - timedelta(seconds=1), 100), Sample(NOW, 0))

    (result,) = averages({"card0": samples}, NOW)

    assert result.windows[0] == WindowAverage("1h", 3600, 0, 0)
    assert result.windows[1].mean == 50  # the 3h window still sees both


def test_a_window_with_no_samples_has_no_mean_and_no_coverage():
    samples = (Sample(NOW - 2 * DAY, 40),)

    (result,) = averages({"card0": samples}, NOW)

    assert result.windows[3] == WindowAverage("24h", 86400, None, 0)
    assert result.windows[4] == WindowAverage("7d", 7 * 86400, 40, 2 * 86400)


def test_a_sample_after_now_is_outside_every_window():
    samples = (Sample(NOW + HOUR, 100), Sample(NOW, 10))

    (result,) = averages({"card0": samples}, NOW)

    assert all(w.mean == 10 for w in result.windows)


def test_coverage_is_now_minus_the_oldest_sample_inside_the_window():
    samples = (Sample(NOW - 3 * DAY, 10), Sample(NOW - 5 * HOUR, 30), Sample(NOW, 50))

    (result,) = averages({"card0": samples}, NOW)
    by_name = {w.window: w for w in result.windows}

    assert by_name["1h"].covered_seconds == 0
    assert by_name["7h"].covered_seconds == 5 * 3600
    assert by_name["24h"].covered_seconds == 5 * 3600
    assert by_name["7d"].covered_seconds == 3 * 86400


def test_averages_of_an_empty_history_is_empty():
    assert averages({}, NOW) == ()


def test_averages_is_a_tuple_of_gpu_averages():
    (result,) = averages({"card0": (Sample(NOW, 1),)}, NOW)

    assert isinstance(result, GpuAverages)
