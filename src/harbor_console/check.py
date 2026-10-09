"""`harbor-console-check`: print the verdict file and exit accordingly.

The third surface over the same verdict, for a terminal: `install.sh` ends
with it so a deploy that leaves the platform broken fails where the
operator is looking, and a human can run it over SSH. No flags; it reads
the one file and judges prober-fresh itself, like every other reader.

Exit 0: fresh and nothing failed. 1: something failed. 2: nothing has
reported, or not for 90 s -- a different problem from a failed check, and
one `install.sh` waits out before giving up.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import datetime
from typing import TextIO

from harbor_console.checks import (
    STATE_FAILED,
    STATE_OK,
    STATE_UNKNOWN,
    freshness_check,
    platform_broken,
)
from harbor_console.verdict import Verdict, read_verdict

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_STALE = 2

_LABELS = {STATE_OK: "PASS", STATE_FAILED: "FAIL", STATE_UNKNOWN: "UNKNOWN"}


def main(
    argv: list[str] | None = None,
    reader: Callable[[], Verdict | None] = read_verdict,
    clock: Callable[[], datetime] = datetime.now,
    out: TextIO = sys.stdout,
) -> int:
    verdict = reader()
    now = clock()
    if verdict is None:
        print("verdict: no verdict has been written yet (is harbor-console-web running?)", file=out)
        return EXIT_STALE

    fresh = freshness_check(verdict.written, now)
    checks = verdict.checks + (fresh,)
    for check in checks:
        line = f"{_LABELS.get(check.state, check.state.upper())} {check.name}"
        if check.state != STATE_OK:
            line += f": {check.reason}"
        print(line, file=out)

    passed = sum(1 for c in checks if c.state == STATE_OK)
    failed = sum(1 for c in checks if c.state == STATE_FAILED)
    unknown = sum(1 for c in checks if c.state == STATE_UNKNOWN)
    counts = [f"{passed} passed"]
    if failed:
        counts.insert(0, f"{failed} failed")
    if unknown:
        counts.append(f"{unknown} unknown")
    headline = "PLATFORM BROKEN" if platform_broken(checks) else "PLATFORM OK"
    print(
        f"verdict: {headline} ({', '.join(counts)}), written {verdict.written:%Y-%m-%d %H:%M:%S}",
        file=out,
    )

    if fresh.state == STATE_FAILED:
        return EXIT_STALE
    if failed:
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
