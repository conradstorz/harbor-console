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


# --- format_averages -------------------------------------------------------

from harbor_console.gpu_history import FULL_COVERAGE, NO_MEAN, format_averages  # noqa: E402


def window(name: str, seconds: int, mean: int | None, covered: int | None = None) -> WindowAverage:
    """A window; coverage defaults to full."""
    return WindowAverage(name, seconds, mean, seconds if covered is None else covered)


def test_format_joins_every_window_in_order():
    entry = GpuAverages(
        "card1",
        (
            window("1h", 3600, 42),
            window("3h", 3 * 3600, 38),
            window("7h", 7 * 3600, 30),
            window("24h", 86400, 25),
            window("7d", 7 * 86400, 18),
        ),
    )

    assert format_averages(entry) == "1h 42% · 3h 38% · 7h 30% · 24h 25% · 7d 18%"


def test_format_flags_a_window_the_samples_do_not_cover():
    entry = GpuAverages("card1", (window("7d", 7 * 86400, 18, covered=2 * 86400),))

    assert format_averages(entry) == "7d 18% (2d)"


def test_format_does_not_flag_coverage_at_or_above_the_threshold():
    assert FULL_COVERAGE == 0.95
    just_enough = GpuAverages("card1", (window("1h", 3600, 5, covered=3420),))
    just_short = GpuAverages("card1", (window("1h", 3600, 5, covered=3419),))

    assert format_averages(just_enough) == "1h 5%"
    assert format_averages(just_short) == "1h 5% (56m)"


def test_format_shows_the_span_in_minutes_hours_or_days_floored():
    cases = [
        (window("1h", 3600, 1, covered=0), "1h 1% (0m)"),
        (window("3h", 3 * 3600, 1, covered=59 * 60 + 59), "3h 1% (59m)"),
        (window("24h", 86400, 1, covered=3600), "24h 1% (1h)"),
        (window("7d", 7 * 86400, 1, covered=23 * 3600 + 3599), "7d 1% (23h)"),
        (window("7d", 7 * 86400, 1, covered=86400), "7d 1% (1d)"),
        (window("7d", 7 * 86400, 1, covered=574559), "7d 1% (6d)"),
    ]

    for w, expected in cases:
        assert format_averages(GpuAverages("card1", (w,))) == expected


def test_format_shows_a_dash_for_a_window_with_no_samples():
    entry = GpuAverages("card1", (window("1h", 3600, 5), WindowAverage("24h", 86400, None, 0)))

    assert NO_MEAN == "—"
    assert format_averages(entry) == "1h 5% · 24h —"


def test_format_fits_the_steady_partial_row_in_the_console_value_column():
    """From one day after a fresh history until the week is full, only the
    7 d window carries a flag. 80 columns minus the panel border, padding
    and the GPU label leaves 54 for the value (see `test_gpu.py`); this
    row has to fit beside the instantaneous one without wrapping."""
    entry = GpuAverages(
        "card1",
        (
            window("1h", 3600, 100),
            window("3h", 3 * 3600, 100),
            window("7h", 7 * 3600, 100),
            window("24h", 86400, 100),
            window("7d", 7 * 86400, 100, covered=6 * 86400),
        ),
    )

    assert format_averages(entry) == "1h 100% · 3h 100% · 7h 100% · 24h 100% · 7d 100% (6d)"
    assert len(format_averages(entry)) <= 54


# --- codec ---------------------------------------------------------------

import json  # noqa: E402

import pytest  # noqa: E402

from harbor_console.gpu_history import (  # noqa: E402
    HISTORY_PATH,
    dumps,
    loads,
    read_history,
    write_history,
)

HISTORY = {
    "card0": (Sample(NOW - HOUR, 10), Sample(NOW, 20)),
    "card1": (Sample(NOW, 99),),
}


def test_history_lives_in_the_web_units_state_directory():
    assert HISTORY_PATH.as_posix() == "/var/lib/harbor-console/gpu-history.json"


def test_codec_round_trip():
    assert loads(dumps(HISTORY)) == HISTORY


def test_dumps_is_compact_json_of_epoch_busy_pairs():
    payload = json.loads(dumps(HISTORY))

    assert payload == {
        "cards": {
            "card0": [[int((NOW - HOUR).timestamp()), 10], [int(NOW.timestamp()), 20]],
            "card1": [[int(NOW.timestamp()), 99]],
        }
    }
    assert "\n" not in dumps(HISTORY)


def test_dumps_drops_sub_second_precision_on_the_way_out():
    history = {"card0": (Sample(NOW.replace(microsecond=500000), 1),)}

    assert loads(dumps(history)) == {"card0": (Sample(NOW, 1),)}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        "{}",
        '{"cards": []}',
        '{"cards": {"card0": "x"}}',
        '{"cards": {"card0": [[1, 2, 3]]}}',
        '{"cards": {"card0": [["1", 2]]}}',
        '{"cards": {"card0": [[1.5, 2]]}}',
        '{"cards": {"card0": [[1, true]]}}',
        '{"cards": {"card0": [[1, 2]], "card1": [[1]]}}',
        '{"cards": {"card0": [[1e20, 2]]}}',
        '{"cards": {7: [[1, 2]]}}',
    ],
)
def test_loads_returns_an_empty_history_for_anything_malformed(text):
    assert loads(text) == {}


def test_loads_keeps_samples_in_file_order():
    text = '{"cards": {"card0": [[1000, 1], [500, 2]]}}'

    assert [s.busy_percent for s in loads(text)["card0"]] == [1, 2]


def test_loads_drops_a_card_with_no_samples():
    assert loads('{"cards": {"card0": []}}') == {}


def test_write_then_read(tmp_path):
    path = tmp_path / "gpu-history.json"

    write_history(HISTORY, path)

    assert read_history(path) == HISTORY


def test_write_leaves_no_temp_file_behind(tmp_path):
    path = tmp_path / "gpu-history.json"

    write_history(HISTORY, path)

    assert [p.name for p in tmp_path.iterdir()] == ["gpu-history.json"]


def test_write_raises_when_the_directory_is_missing(tmp_path):
    with pytest.raises(OSError):
        write_history(HISTORY, tmp_path / "missing" / "gpu-history.json")


def test_read_is_empty_for_a_missing_file(tmp_path):
    assert read_history(tmp_path / "missing.json") == {}


def test_read_is_empty_for_garbage(tmp_path):
    path = tmp_path / "gpu-history.json"
    path.write_text("{{{", encoding="utf-8")

    assert read_history(path) == {}


def test_read_defaults_to_the_state_directory_path():
    import inspect

    assert inspect.signature(read_history).parameters["path"].default is HISTORY_PATH
    assert inspect.signature(write_history).parameters["path"].default is HISTORY_PATH
