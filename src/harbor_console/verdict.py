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
from pathlib import Path

from harbor_console.checks import STATE_FAILED, STATE_OK, STATE_UNKNOWN, Check
from harbor_console.gpu_history import GpuAverages, WindowAverage

_VALID_STATES = (STATE_OK, STATE_FAILED, STATE_UNKNOWN)

VERDICT_PATH = Path("/run/harbor-console/checks.json")


@dataclass(frozen=True)
class Verdict:
    """One cycle's checks, stamped with when the prober wrote them, and the
    GPU busy averages the prober keeps (ADR 22): the console shows them
    under each card and computes nothing."""

    written: datetime
    hostname: str
    checks: tuple[Check, ...]
    gpus: tuple[GpuAverages, ...] = ()


def dumps(verdict: Verdict) -> str:
    return json.dumps(
        {
            "written": verdict.written.isoformat(),
            "hostname": verdict.hostname,
            "checks": [
                {"name": c.name, "state": c.state, "reason": c.reason} for c in verdict.checks
            ],
            "gpus": [
                {
                    "card": g.card,
                    "windows": [
                        {
                            "window": w.window,
                            "seconds": w.seconds,
                            "mean": w.mean,
                            "covered_seconds": w.covered_seconds,
                        }
                        for w in g.windows
                    ],
                }
                for g in verdict.gpus
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
        hostname = payload["hostname"]
        if not isinstance(hostname, str):
            return None
        raw_checks = payload["checks"]
        if not isinstance(raw_checks, list):
            return None
        checks = []
        for c in raw_checks:
            if not isinstance(c, dict):
                return None
            name, state, reason = c["name"], c["state"], c["reason"]
            if not (isinstance(name, str) and isinstance(state, str) and isinstance(reason, str)):
                return None
            if state not in _VALID_STATES:
                return None
            checks.append(Check(name=name, state=state, reason=reason))
        checks = tuple(checks)
        gpus = _load_gpus(payload.get("gpus", []))
    except (KeyError, TypeError, ValueError):
        return None
    return Verdict(written=written, hostname=hostname, checks=checks, gpus=gpus)


def _load_gpus(raw: object) -> tuple[GpuAverages, ...]:
    """The `gpus` list, or a `ValueError` for anything that is not one; a
    missing key is passed in as `[]` by the caller, so an older writer
    still loads."""
    if not isinstance(raw, list):
        raise ValueError("gpus is not a list")
    entries = []
    for g in raw:
        if not isinstance(g, dict) or not isinstance(g["card"], str) or not isinstance(g["windows"], list):
            raise ValueError("malformed gpu entry")
        windows = []
        for w in g["windows"]:
            if not isinstance(w, dict):
                raise ValueError("malformed window")
            window, seconds, mean, covered = w["window"], w["seconds"], w["mean"], w["covered_seconds"]
            if not (isinstance(window, str) and _is_int(seconds) and _is_int(covered)):
                raise ValueError("malformed window")
            if mean is not None and not _is_int(mean):
                raise ValueError("malformed window")
            windows.append(WindowAverage(window, seconds, mean, covered))
        entries.append(GpuAverages(g["card"], tuple(windows)))
    return tuple(entries)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def read_verdict(path: Path = VERDICT_PATH) -> Verdict | None:
    """The verdict on disk, or None. Never raises: a missing, unreadable or undecodable file is None; never blocks on anything but a local file."""
    try:
        return loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_verdict(verdict: Verdict, path: Path = VERDICT_PATH) -> None:
    """Replace the file atomically. Raises OSError; the caller reports it."""
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(dumps(verdict), encoding="utf-8")
    os.replace(temp, path)
