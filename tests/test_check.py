import io
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

from harbor_console import check
from harbor_console.checks import STATE_FAILED, STATE_OK, STATE_UNKNOWN, Check
from harbor_console.verdict import Verdict

NOW = datetime(2026, 10, 9, 17, 21, 46)


def run(verdict, now=NOW):
    out = io.StringIO()
    code = check.main(reader=lambda: verdict, clock=lambda: now, out=out)
    return code, out.getvalue()


def test_healthy_exits_zero_and_lists_every_check():
    verdict = Verdict(NOW, "hpz440", (
        Check("docker", STATE_OK, "Docker answered"),
        Check("certificate", STATE_UNKNOWN, "not checked: no tailnet address"),
    ))

    code, text = run(verdict)

    assert code == check.EXIT_OK
    assert "PASS docker\n" in text
    assert "UNKNOWN certificate: not checked: no tailnet address\n" in text
    assert "PASS prober-fresh" in text
    assert text.rstrip().endswith("verdict: PLATFORM OK (2 passed, 1 unknown), written 2026-10-09 17:21:46")


def test_a_failed_check_exits_one():
    verdict = Verdict(NOW, "hpz440", (Check("own-route", STATE_FAILED, "504"),))

    code, text = run(verdict)

    assert code == check.EXIT_FAILED
    assert "FAIL own-route: 504\n" in text
    assert "verdict: PLATFORM BROKEN (1 failed" in text


def test_a_stale_verdict_exits_two():
    verdict = Verdict(NOW - timedelta(minutes=5), "hpz440", (Check("docker", STATE_OK, "x"),))

    code, text = run(verdict)

    assert code == check.EXIT_STALE
    assert "FAIL prober-fresh: status page has not reported since 17:16:46" in text


def test_a_stale_verdict_with_another_failure_still_exits_two():
    verdict = Verdict(
        NOW - timedelta(minutes=5),
        "hpz440",
        (Check("own-route", STATE_FAILED, "504"),),
    )

    code, text = run(verdict)

    assert code == check.EXIT_STALE
    assert "FAIL own-route: 504\n" in text
    assert "FAIL prober-fresh: status page has not reported since 17:16:46" in text


def test_a_missing_verdict_exits_two():
    code, text = run(None)

    assert code == check.EXIT_STALE
    assert "no verdict" in text


def test_the_console_script_is_registered():
    pyproject = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())

    assert pyproject["project"]["scripts"]["harbor-console-check"] == "harbor_console.check:main"
