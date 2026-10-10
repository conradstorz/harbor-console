import json
from datetime import datetime

import pytest

from harbor_console.checks import STATE_FAILED, STATE_OK, Check
from harbor_console.gpu_history import GpuAverages, WindowAverage
from harbor_console.verdict import (
    VERDICT_PATH,
    Verdict,
    dumps,
    loads,
    read_verdict,
    write_verdict,
)

VERDICT = Verdict(
    written=datetime(2026, 10, 9, 17, 21, 46),
    hostname="hpz440",
    checks=(
        Check("docker", STATE_OK, "Docker answered"),
        Check("own-route", STATE_FAILED, "https://harbor.hpz440.ohr3023.org/ did not answer"),
    ),
)

AVERAGES = (
    GpuAverages(
        "card1",
        (
            WindowAverage("1h", 3600, 42, 3600),
            WindowAverage("7d", 7 * 86400, None, 0),
        ),
    ),
)
WITH_GPUS = Verdict(
    written=VERDICT.written,
    hostname=VERDICT.hostname,
    checks=VERDICT.checks,
    gpus=AVERAGES,
)


def test_round_trip():
    assert loads(dumps(VERDICT)) == VERDICT


def test_dumps_is_plain_json_with_isoformat_timestamp():
    payload = json.loads(dumps(VERDICT))

    assert payload["written"] == "2026-10-09T17:21:46"
    assert payload["hostname"] == "hpz440"
    assert payload["checks"][1] == {
        "name": "own-route",
        "state": "failed",
        "reason": "https://harbor.hpz440.ohr3023.org/ did not answer",
    }


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        '{"written": "2026-10-09T17:21:46"}',
        '{"written": "yesterday", "hostname": "h", "checks": []}',
        '{"written": "2026-10-09T17:21:46", "hostname": "h", "checks": [{"name": "x"}]}',
        '{"written": "2026-10-09T17:21:46", "hostname": "h", "checks": "none"}',
        '{"written": "2026-10-09T17:21:46", "hostname": "h", "checks": [{"name": "x", "state": "bogus", "reason": "r"}]}',
        '{"written": "2026-10-09T17:21:46", "hostname": "h", "checks": [{"name": 1, "state": "ok", "reason": "r"}]}',
        '{"written": "2026-10-09T17:21:46", "hostname": 7, "checks": []}',
    ],
)
def test_loads_returns_none_for_malformed_input(text):
    assert loads(text) is None


def test_write_then_read(tmp_path):
    path = tmp_path / "checks.json"

    write_verdict(VERDICT, path)

    assert read_verdict(path) == VERDICT


def test_write_leaves_no_temp_file_behind(tmp_path):
    path = tmp_path / "checks.json"

    write_verdict(VERDICT, path)

    assert [p.name for p in tmp_path.iterdir()] == ["checks.json"]


def test_write_raises_when_the_directory_is_missing(tmp_path):
    with pytest.raises(OSError):
        write_verdict(VERDICT, tmp_path / "missing" / "checks.json")


def test_read_returns_none_when_the_file_is_missing(tmp_path):
    assert read_verdict(tmp_path / "checks.json") is None


def test_read_returns_none_on_garbage(tmp_path):
    path = tmp_path / "checks.json"
    path.write_text("{", encoding="utf-8")

    assert read_verdict(path) is None


def test_read_returns_none_on_bytes_that_are_not_utf8(tmp_path):
    path = tmp_path / "checks.json"
    path.write_bytes(b"\xff\xfe\x00garbage")

    assert read_verdict(path) is None


def test_the_default_path_is_under_run():
    assert VERDICT_PATH.as_posix() == "/run/harbor-console/checks.json"


def test_read_with_the_default_path_degrades_when_the_file_is_missing():
    # The default must be a real Path: a PurePosixPath has no read_text and
    # would raise AttributeError here, which read_verdict does not catch.
    assert read_verdict() is None


def test_round_trip_with_gpu_averages():
    assert loads(dumps(WITH_GPUS)) == WITH_GPUS


def test_dumps_writes_the_averages_as_plain_json():
    payload = json.loads(dumps(WITH_GPUS))

    assert payload["gpus"] == [
        {
            "card": "card1",
            "windows": [
                {"window": "1h", "seconds": 3600, "mean": 42, "covered_seconds": 3600},
                {"window": "7d", "seconds": 604800, "mean": None, "covered_seconds": 0},
            ],
        }
    ]


def test_a_verdict_without_gpus_has_none():
    assert VERDICT.gpus == ()
    assert json.loads(dumps(VERDICT))["gpus"] == []


def test_loads_treats_a_missing_gpus_key_as_empty():
    """An older writer and a newer reader coexist across a deploy."""
    payload = json.loads(dumps(VERDICT))
    del payload["gpus"]

    assert loads(json.dumps(payload)) == VERDICT


@pytest.mark.parametrize(
    "gpus",
    [
        "none",
        [1],
        [{"card": "card1"}],
        [{"card": 1, "windows": []}],
        [{"card": "card1", "windows": "x"}],
        [{"card": "card1", "windows": [{"window": "1h"}]}],
        [{"card": "card1", "windows": [{"window": "1h", "seconds": "3600", "mean": 1, "covered_seconds": 0}]}],
        [{"card": "card1", "windows": [{"window": "1h", "seconds": 3600, "mean": "1", "covered_seconds": 0}]}],
        [{"card": "card1", "windows": [{"window": "1h", "seconds": 3600, "mean": 1, "covered_seconds": None}]}],
        [{"card": "card1", "windows": [{"window": 1, "seconds": 3600, "mean": 1, "covered_seconds": 0}]}],
    ],
)
def test_loads_returns_none_for_malformed_gpus(gpus):
    payload = json.loads(dumps(VERDICT))
    payload["gpus"] = gpus

    assert loads(json.dumps(payload)) is None
