"""Liveness and optional detail for one declared service.

Deliberately dumb: connect, and any HTTP response means up. GTE answers `/`
with a 303 to `/login`; a probe insisting on 200 would call a healthy service
down, and a status page that cries wolf is worse than no status page.

`/hcstatus` only ever adds detail. A project whose endpoint is missing, slow,
malformed or wrongly shaped still shows as up -- with a warning where the
project got it wrong, and silently where it simply does not offer one.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

HCSTATUS_PATH = "/hcstatus"
VALID_STATES = ("ok", "warn", "error")


@dataclass(frozen=True)
class Detail:
    """One label/value row a project chose to publish."""

    label: str
    value: str


@dataclass(frozen=True)
class Health:
    """What one probe learned. `warning` explains an ignored /hcstatus."""

    up: bool
    state: str | None
    summary: str | None
    detail: tuple[Detail, ...]
    warning: str | None


def probe(
    host: str,
    port: int,
    opener: Callable[..., object] = urllib.request.urlopen,
    timeout: float = 2.0,
) -> Health:
    """Probe one service for liveness, then for optional detail."""
    base = f"http://{host}:{port}"

    if not _answers(f"{base}/", opener, timeout):
        return Health(up=False, state=None, summary=None, detail=(), warning=None)

    state, summary, detail, warning = _hcstatus(
        f"{base}{HCSTATUS_PATH}", opener, timeout
    )
    return Health(up=True, state=state, summary=summary, detail=detail, warning=warning)


def _answers(url: str, opener: Callable[..., object], timeout: float) -> bool:
    """True when anything answers over HTTP, including an error status.

    The whole call is somebody else's code -- a platform-dependent transport
    -- so the catch here is deliberately broad: any way it can fail, short of
    an actual response, means down.
    """
    try:
        with opener(url, timeout=timeout):  # type: ignore[union-attr]
            return True
    except urllib.error.HTTPError:
        # A 404 or a 500 is still a service answering.
        return True
    except (OSError, ValueError, http.client.HTTPException):
        return False


def _hcstatus(
    url: str, opener: Callable[..., object], timeout: float
) -> tuple[str | None, str | None, tuple[Detail, ...], str | None]:
    """Fetch and validate /hcstatus. Never decides up or down.

    The fetch is guarded broadly, because it is the external call -- the
    injected opener and reading its response -- and any way that can fail is
    this module's problem, not a bug in it. A 404 is the ordinary case of a
    project simply not offering the endpoint, so it alone produces no
    warning; every other failure to fetch does. Parsing and validating the
    bytes once they are in hand is this module's own logic, so it gets its
    own narrow handling below, outside this block.
    """
    try:
        with opener(url, timeout=timeout) as response:  # type: ignore[union-attr]
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # Not offering /hcstatus is the ordinary case, not a fault.
            return None, None, (), None
        return None, None, (), f"{HCSTATUS_PATH} returned {exc.code}"
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return None, None, (), f"{HCSTATUS_PATH} unreadable: {exc}"

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, None, (), f"{HCSTATUS_PATH} unreadable: {exc}"

    if not isinstance(payload, dict):
        return None, None, (), f"{HCSTATUS_PATH} is not a JSON object"

    state = payload.get("state")
    if state is not None and state not in VALID_STATES:
        return None, None, (), f"{HCSTATUS_PATH} state '{state}' is not ok/warn/error"

    summary = payload.get("summary")
    if summary is not None and not isinstance(summary, str):
        return None, None, (), f"{HCSTATUS_PATH} summary is not a string"

    rows = payload.get("detail", [])
    if not isinstance(rows, list):
        return state, summary, (), f"{HCSTATUS_PATH} detail is not a list"

    detail = []
    dropped = False
    for row in rows:
        if (
            isinstance(row, dict)
            and isinstance(row.get("label"), str)
            and isinstance(row.get("value"), (str, int, float))
        ):
            detail.append(Detail(label=row["label"], value=str(row["value"])))
        else:
            dropped = True

    warning = (
        f"{HCSTATUS_PATH} had detail rows that were not label/value"
        if dropped
        else None
    )
    return state, summary, tuple(detail), warning
