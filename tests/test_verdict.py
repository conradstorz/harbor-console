import json
from datetime import datetime

import pytest

from harbor_console.checks import STATE_FAILED, STATE_OK, Check
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


def test_the_default_path_is_under_run():
    assert VERDICT_PATH.as_posix() == "/run/harbor-console/checks.json"


def test_read_with_the_default_path_degrades_when_the_file_is_missing():
    # The default must be a real Path: a PurePosixPath has no read_text and
    # would raise AttributeError here, which read_verdict does not catch.
    assert read_verdict() is None
