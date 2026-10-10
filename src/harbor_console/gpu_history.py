"""A week of GPU busy samples, and the averages both surfaces show over it.

Policy and codec, in the shape of `verdict.py`. The web prober is the one
writer: each cycle it records one busy sample per card, prunes what is
older than a week, and hands the averages to the snapshot and the verdict
file. The console only ever sees the averages, inside the file it already
reads, so the 1 Hz loop gains no write and no new read (ADR 22).

Everything here is pure in `now`: the prober passes `snapshot.collected`,
and tests pass fixed datetimes.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from harbor_console.gpu import GpuEntry

#: Samples older than this are dropped on every record.
RETENTION = timedelta(days=7)

#: The averages shown, in row order: name and length in seconds.
WINDOWS: tuple[tuple[str, int], ...] = (
    ("1h", 3600),
    ("3h", 3 * 3600),
    ("7h", 7 * 3600),
    ("24h", 86400),
    ("7d", 7 * 86400),
)

#: Below this share of a window, the span the samples cover is shown
#: beside the mean, so a fresh history never passes for a full one.
FULL_COVERAGE = 0.95

#: What a window with no samples shows in place of a percentage.
NO_MEAN = "—"

#: The averages row's label. Shared by both renderers.
AVERAGES_LABEL = "busy avg"
#: What a card with no verdict entry shows in place of averages.
NOT_REPORTED = "not reported"

#: Under the web unit's `StateDirectory`: the one place this project keeps
#: anything across a restart or a reboot (ADR 22). No flag moves it.
HISTORY_PATH = Path("/var/lib/harbor-console/gpu-history.json")

_CARD_NUMBER = re.compile(r"(\d+)$")


@dataclass(frozen=True)
class Sample:
    at: datetime
    busy_percent: int


#: History key (the card's PCI bus ID where known, else its node name, see
#: `history_key`) to its samples, oldest first.
History = dict[str, tuple[Sample, ...]]


@dataclass(frozen=True)
class WindowAverage:
    """One window's mean, and how much of the window the samples span.

    `mean` is `None` when no sample fell inside the window. `covered_seconds`
    is `now` minus the oldest sample inside the window, 0 when there is none:
    the honest cheap measure of how much of the window the mean speaks for.
    """

    window: str
    seconds: int
    mean: int | None
    covered_seconds: int


@dataclass(frozen=True)
class GpuAverages:
    key: str
    windows: tuple[WindowAverage, ...]


def history_key(entry: GpuEntry) -> str:
    """The stable key history is kept under: the card's PCI slot where it has
    one, else its DRM node name.

    `card<N>` is assigned in probe order and can change across a reboot --
    exactly what history must not be keyed by, since it is meant to outlive
    one. The PCI slot is the card's address on the bus and does not move. A
    card not on PCI (or whose `uevent` could not be read) has no bus ID, so
    it falls back to its node name, with that name's instability as the
    known limitation for that case.
    """
    return entry.bus_id or entry.card


def record(history: History, entries: tuple[GpuEntry, ...], now: datetime) -> History:
    """`history` plus one sample at `now` per entry that reported a busy
    percent, minus everything older than `RETENTION`. A new mapping; the
    input is never touched.

    An entry with no key (the `unavailable` sentinel, which has neither a
    `card` nor a `bus_id`) or no busy percent (a driver that stayed silent
    this cycle) adds nothing: silence is never a zero. A card with no
    samples left is dropped.
    """
    cutoff = now - RETENTION
    updated: dict[str, tuple[Sample, ...]] = dict(history)
    for entry in entries:
        key = history_key(entry)
        if not key or entry.busy_percent is None:
            continue
        updated[key] = updated.get(key, ()) + (Sample(now, entry.busy_percent),)
    pruned = {
        key: tuple(sample for sample in samples if sample.at >= cutoff)
        for key, samples in updated.items()
    }
    return {key: samples for key, samples in pruned.items() if samples}


def averages(history: History, now: datetime) -> tuple[GpuAverages, ...]:
    """Every window for every card in `history`, in a stable order."""
    return tuple(
        GpuAverages(
            key,
            tuple(_window(history[key], name, seconds, now) for name, seconds in WINDOWS),
        )
        for key in sorted(history, key=_card_order)
    )


def _window(samples: tuple[Sample, ...], name: str, seconds: int, now: datetime) -> WindowAverage:
    start = now - timedelta(seconds=seconds)
    inside = [sample for sample in samples if start <= sample.at <= now]
    if not inside:
        return WindowAverage(name, seconds, None, 0)
    oldest = min(sample.at for sample in inside)
    mean = round(sum(sample.busy_percent for sample in inside) / len(inside))
    return WindowAverage(name, seconds, mean, int((now - oldest).total_seconds()))


def _card_order(card: str) -> tuple[int, str]:
    """`card2` before `card10`: numeric where there is a number, name otherwise."""
    match = _CARD_NUMBER.search(card)
    return (int(match.group(1)) if match else -1, card)


def format_averages(entry: GpuAverages) -> str:
    """`1h 42% · 3h 38% · 7h 30% · 24h 25% · 7d 18% (2d)`.

    A window the samples cover less than `FULL_COVERAGE` of carries the span
    they do cover, floored to the coarsest whole unit: minutes under an
    hour, hours under a day, days beyond. A window with no samples shows
    `NO_MEAN` and no span. Shared by both renderers, like `format_gpu`.
    """
    return " · ".join(_format_window(w) for w in entry.windows)


def _format_window(w: WindowAverage) -> str:
    if w.mean is None:
        return f"{w.window} {NO_MEAN}"
    text = f"{w.window} {w.mean}%"
    if w.covered_seconds < FULL_COVERAGE * w.seconds:
        text += f" ({_span(w.covered_seconds)})"
    return text


def _span(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def dumps(history: History) -> str:
    """`{"cards": {"0000:02:00.0": [[epoch_seconds, busy_percent], ...]}}`,
    on one line: at ~20k pairs per card a week, the file is rewritten every
    cycle and indentation would double it for nothing. The top-level key
    stays `"cards"` for the file shape; what each entry is keyed by is the
    bus ID where `history_key` has one, the card's node name otherwise."""
    return json.dumps(
        {
            "cards": {
                key: [[int(sample.at.timestamp()), sample.busy_percent] for sample in samples]
                for key, samples in history.items()
            }
        },
        separators=(",", ":"),
    )


def loads(text: str) -> History:
    """Parse a history, or an empty one for anything that is not a history.
    Never raises: a malformed file costs the week it held, and the next
    cycle starts a new one."""
    try:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            return {}
        cards = payload["cards"]
        if not isinstance(cards, dict):
            return {}
        history: History = {}
        for key, pairs in cards.items():
            if not isinstance(pairs, list):
                return {}
            samples = []
            for pair in pairs:
                if not (isinstance(pair, list) and len(pair) == 2):
                    return {}
                at, busy = pair
                if not (_is_int(at) and _is_int(busy)):
                    return {}
                if not (0 <= busy <= 100):
                    return {}
                samples.append(Sample(datetime.fromtimestamp(at), busy))
            if samples:
                history[key] = tuple(samples)
        return history
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return {}


def _is_int(value: object) -> bool:
    """`True` for an int that is not a bool; JSON `true` parses as a bool
    and a bool is an int to `isinstance`."""
    return isinstance(value, int) and not isinstance(value, bool)


def read_history(path: Path = HISTORY_PATH) -> History:
    """The history on disk, or an empty one. Never raises."""
    try:
        return loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_history(history: History, path: Path = HISTORY_PATH) -> None:
    """Replace the file atomically. Raises OSError; the caller reports it."""
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(dumps(history), encoding="utf-8")
    os.replace(temp, path)
