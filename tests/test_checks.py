from datetime import datetime, timedelta, timezone

from harbor_console.checks import (
    CHECK_PROBER_FRESH,
    STALE_AFTER_SECONDS,
    STATE_FAILED,
    STATE_OK,
    STATE_UNKNOWN,
    Check,
    freshness_check,
    is_stale,
    platform_broken,
)

# Timezone-aware so the certificate day counts in Task 3 do not depend on
# the workstation's local timezone.
NOW = datetime(2026, 10, 9, 17, 21, 46, tzinfo=timezone.utc)


def test_platform_broken_when_any_check_failed():
    checks = (
        Check("docker", STATE_OK, "answered"),
        Check("own-route", STATE_FAILED, "504"),
    )

    assert platform_broken(checks) is True


def test_platform_not_broken_by_unknown_checks():
    checks = (
        Check("docker", STATE_OK, "answered"),
        Check("certificate", STATE_UNKNOWN, "no tailnet address"),
    )

    assert platform_broken(checks) is False


def test_platform_not_broken_with_no_checks():
    assert platform_broken(()) is False


def test_is_stale_at_the_threshold():
    written = NOW - timedelta(seconds=STALE_AFTER_SECONDS)

    assert is_stale(written, NOW) is False
    assert is_stale(written - timedelta(seconds=1), NOW) is True


def test_freshness_check_is_unknown_without_a_verdict():
    check = freshness_check(None, NOW)

    assert check.name == CHECK_PROBER_FRESH
    assert check.state == STATE_UNKNOWN


def test_freshness_check_fails_when_stale():
    check = freshness_check(NOW - timedelta(seconds=200), NOW)

    assert check.state == STATE_FAILED
    assert "200 s ago" in check.reason


def test_freshness_check_passes_when_recent():
    check = freshness_check(NOW - timedelta(seconds=12), NOW)

    assert check.state == STATE_OK
    assert "12 s ago" in check.reason
