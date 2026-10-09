"""The verdict file: how the checks cross from the web prober to the console.

A contract, like `snapshot.py`, with the one addition that this one crosses
a process boundary, so it carries its own codec and the two file calls.
No policy: what the checks mean is `checks.py`'s business, and what to show
is the renderers'.

The file lives under `/run`, a tmpfs the web unit owns through systemd's
`RuntimeDirectory`, so a reboot starts clean and nothing persists a verdict
from a previous life of the host. Written atomically -- a temp name in the
same directory, then `os.replace` -- so a reader never sees half a file.
`loads` and `read_verdict` never raise: a missing, unreadable or malformed
file is `None`, and every reader treats `None` as "nothing has reported".
`write_verdict` does raise, because its one caller decides how to report a
write that failed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from harbor_console.checks import Check

VERDICT_PATH = PurePosixPath("/run/harbor-console/checks.json")


@dataclass(frozen=True)
class Verdict:
    """One cycle's checks, stamped with when the prober wrote them."""

    written: datetime
    hostname: str
    checks: tuple[Check, ...]


def dumps(verdict: Verdict) -> str:
    return json.dumps(
        {
            "written": verdict.written.isoformat(),
            "hostname": verdict.hostname,
            "checks": [
                {"name": c.name, "state": c.state, "reason": c.reason} for c in verdict.checks
            ],
        },
        indent=2,
    )


def loads(text: str) -> Verdict | None:
    """Parse a verdict, or None for anything that is not one. Never raises."""
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        written = datetime.fromisoformat(str(payload["written"]))
        hostname = str(payload["hostname"])
        raw_checks = payload["checks"]
        if not isinstance(raw_checks, list):
            return None
        checks = tuple(
            Check(name=str(c["name"]), state=str(c["state"]), reason=str(c["reason"]))
            for c in raw_checks
        )
    except (KeyError, TypeError, ValueError):
        return None
    return Verdict(written=written, hostname=hostname, checks=checks)


def read_verdict(path: Path = VERDICT_PATH) -> Verdict | None:
    """The verdict on disk, or None. Never raises; never blocks on anything but a local file."""
    try:
        return loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def write_verdict(verdict: Verdict, path: Path = VERDICT_PATH) -> None:
    """Replace the file atomically. Raises OSError; the caller reports it."""
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(dumps(verdict), encoding="utf-8")
    os.replace(temp, path)
