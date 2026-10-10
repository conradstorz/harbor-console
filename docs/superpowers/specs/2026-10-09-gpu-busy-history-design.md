# GPU busy history: averages over 1 h, 3 h, 7 h, 24 h and 7 d

Date: 2026-10-09

## Goal

Both surfaces show, under each GPU row, the card's mean busy percent over
the last hour, 3 hours, 7 hours, 24 hours and week, beside the instantaneous
row they already show. Busy percent only: VRAM, temperature, power and fan
stay instantaneous.

A week of history has to outlive both processes. The console restarts on
every tty1 login, the web prober on every deploy, and `/run` is wiped on
reboot. This is therefore the project's first persistent state, and it gets
an ADR (ADR 22) amending the "no persistence" stance and ADR 21's statement
that the verdict file carries checks only.

## Approach

The web prober samples and persists; the console only reads.

The prober already wakes every 30 s and is the long-lived process. Each cycle
it appends one busy sample per card to a history file under the web unit's
systemd `StateDirectory`, prunes anything older than a week, computes the five
averages and writes them into the verdict file the console already reads once
per tick. The console stays a 1 Hz reader that never blocks and never writes.

Rejected: a shared history file both processes write (two writers need
locking, and the console would gain a disk write per second, which breaks
"never stalls"); in-memory history (a 7 d average needs 7 d of uptime from
one process, and the console does not have it).

## Components and data flow

- `gpu.py` -- `GpuEntry` gains `card: str` (`card1`), the stable key for
  history. The label can change when NVML goes quiet for a cycle; the card
  name cannot. `collect_gpus` fills it from the node name. The `unavailable`
  sentinel entry has an empty `card`.
- `gpu_history.py` (new) -- pure policy plus codec, in the shape of
  `verdict.py`:
  - `Sample(at: datetime, busy_percent: int)`.
  - `History = dict[str, tuple[Sample, ...]]`, card name to samples, oldest
    first.
  - `record(history, entries, now) -> History`: for every entry with a
    non-empty `card` and a non-`None` `busy_percent`, append a sample at
    `now`; then drop every sample older than `now - RETENTION` (7 d) from
    every card. Returns a new mapping; never mutates.
  - `WindowAverage(window: str, seconds: int, mean: int | None,
    covered_seconds: int)`: `mean` is the plain mean of the samples inside
    `now - seconds`, rounded to a whole percent, `None` when there are none;
    `covered_seconds` is `now` minus the oldest sample inside the window,
    `0` when there are none.
  - `GpuAverages(key: str, windows: tuple[WindowAverage, ...])`.
  - `WINDOWS = (("1h", 3600), ("3h", 3 * 3600), ("7h", 7 * 3600),
    ("24h", 86400), ("7d", 7 * 86400))`.
  - `averages(history, now) -> tuple[GpuAverages, ...]`: one entry per card
    in the history, in card order, each with all five windows.
  - `format_averages(entry: GpuAverages) -> str`: see Rendering.
  - `dumps(history) -> str`, `loads(text) -> History` (never raises; garbage
    is an empty history), `read_history(path) -> History` (never raises),
    `write_history(history, path)` (atomic: temp name in the same directory
    then `os.replace`; raises `OSError`, the caller reports).
- `webapp.py` -- `GpuHistoryKeeper`, the stateful counterpart of
  `VerdictPublisher`. Constructed with the path, a reader, a writer and a
  reporter. Loads the file once in `__init__`. `__call__(entries, now)`
  records, writes, and returns `averages(history, now)`. A write that fails
  is reported once per distinct error, never raised, and the in-memory
  history still advances so the page keeps its averages until the disk
  recovers. `collect_snapshot` gains `history: Callable[[tuple[GpuEntry,
  ...], datetime], tuple[GpuAverages, ...]]`, defaulting to a function that
  returns `()`; `_default_prober` constructs one keeper and wires it.
- `snapshot.py` -- gains `gpu_averages: tuple[GpuAverages, ...] = ()`.
- `verdict.py` -- `Verdict` gains `gpus: tuple[GpuAverages, ...] = ()`,
  serialised as `"gpus": [{"key": ..., "windows": [{"window": "1h",
  "seconds": 3600, "mean": 42, "covered_seconds": 3600}, ...]}]`. `loads`
  treats a missing `gpus` key as empty, so an old writer and a new reader
  coexist across a deploy; a present but malformed `gpus` makes the whole
  verdict `None`, as any other malformed field does. `VerdictPublisher`
  copies `snapshot.gpu_averages` in.
- `app.py` / `ui.py` -- `build_dashboard` gains `gpu_averages: tuple[
  GpuAverages, ...] = ()`; `app.run` passes the verdict's `gpus` (empty when
  the verdict is `None`). `ui` matches entries to cards by `card`.
- `web.py` -- `_gpu_section` renders the same second row from
  `snapshot.gpu_averages`.

The console keeps zero writes and zero new reads: the averages arrive inside
the file it already reads.

## The history file

- Path: `/var/lib/harbor-console/gpu-history.json`, from
  `StateDirectory=harbor-console` added to `harbor-console-web.service`.
  systemd creates it owned by `harbor`. `HISTORY_PATH` is a constant in
  `gpu_history.py`; no flag moves it.
- Shape: `{"cards": {"card1": [[epoch_seconds, busy_percent], ...]}}`.
  Integer epoch seconds, oldest first.
- One pair per card per prober cycle, only when the driver reported a busy
  percent. A cycle where NVML stayed silent adds nothing; silence is never a
  zero.
- Retention: everything older than 7 d from `now` is dropped on every record.
  At 30 s cycles that is about 20k pairs per card, about 400 KB of JSON,
  rewritten atomically every cycle (about 1 GB/day, negligible on an SSD and
  simpler than batching writes).
- Loaded once at startup. Missing, unreadable or malformed means an empty
  history, and the next cycle starts it fresh. Nothing is reported: there is
  nothing an operator could do about it, and the write path reports its own
  failures.
- `uninstall.sh --purge` removes `/var/lib/harbor-console`, as it does the
  install directory and the `harbor` user; a plain uninstall keeps the week
  of history for a reinstall.

## Averaging rules

- Windows: 1 h, 3 h, 7 h, 24 h, 7 d.
- Each is the plain mean of the samples whose timestamp satisfies
  `now - window <= at <= now`, rounded to a whole percent with Python's
  `round`. No weighting, no downsampling.
- Coverage: `now` minus the oldest sample inside the window. A gap in the
  middle (the host was down) is not flagged; the span is the honest cheap
  measure and the mean is still a mean over real samples.
- `now` is the prober's `snapshot.collected`, injected, so every rule is
  testable with fixed datetimes. A clock that jumps backwards leaves
  future-stamped samples in place; they fall outside every window (they are
  after `now`) and are pruned only by age, so nothing is deleted on that
  account.

## Rendering

- `format_averages` renders every window joined by ` · `:
  `1h 42% · 3h 38% · 7h 30% · 24h 25% · 7d 18% (2d)`.
  - A window with no samples renders `—` in place of the percent, no
    coverage.
  - Coverage is appended in parentheses when `covered_seconds` is under 95%
    of the window: minutes under an hour (`40m`), whole hours under a day
    (`5h`), days otherwise (`2d`), each `floor`ed. A fresh history flags
    every window for its first 57 minutes (95% of an hour), and two or more
    windows for its first day; the Width paragraph below describes what
    that costs.
  - Width. Unflagged, the row is at most 48 cells (`1h 100% · 3h 100% ·
    7h 100% · 24h 100% · 7d 100%`). With only the 7 d window flagged, the
    state the row is in from one day after a fresh history until the week
    is full, it is at most 53, and that case is pinned by a test to the
    console's 54-cell value width. During the first day of a fresh history
    two or more windows carry a flag and the row wraps onto a second line on
    the console; that is the accepted cost ADR 20 names, and it ends on its
    own.
- Console: under each GPU row, one row labelled `  busy avg` (two leading
  spaces, so it reads as belonging to the card above). Value: the formatted
  averages of the verdict entry whose `card` matches; `not reported` when
  there is no verdict or no entry for that card. Staleness stays the
  banner's job; a stale verdict still renders its last averages. With one
  card hpz440 spends one of its three spare rows; while the banner is up
  (it takes all three) the panel's bottom border is cropped, and the remedy
  ADR 20 names is the smaller console font, not compressing rows.
- Web page: the GPU table gets the same second row per card from
  `snapshot.gpu_averages`, same label, same `not reported` fallback (on the
  web that means the keeper had nothing yet).
- `No GPU detected` and `unavailable` entries get no averages row: there is no
  card to average.
- Nothing else changes: no colour, no sparkline, no per-window columns.

## Deployment

- `deploy/harbor-console-web.service`: `StateDirectory=harbor-console`, with
  a comment saying why it is the one persistent directory.
- `deploy/uninstall.sh`: `rm -rf /var/lib/harbor-console` beside the other
  removals.
- `install.sh` needs nothing: systemd creates the directory.
- ADR 22, "Keep a week of GPU busy history in the web unit's state
  directory": records the persistence decision, the single-writer rule, and
  that the verdict file now carries the averages as well as the checks.
- `CLAUDE.md`: `gpu_history.py` in the module list; the load-bearing
  behaviours gain the single-writer rule; the scope paragraph's "no
  persistence" becomes "no persistence but the GPU history file (ADR 22)".

## Testing

Plain values, fixed datetimes, no real time, sockets, Docker or disk except
`tmp_path`.

- `tests/test_gpu_history.py`: `record` appends only cards with a busy
  percent, prunes by age, never mutates its input; `averages` over
  hand-built samples for each window including empty, partial and full
  coverage; `format_averages` for the plain, flagged, empty and
  fullest-flagged (width-pinned) cases; codec round trip; `loads` on
  garbage, wrong shapes, non-integer pairs returns empty; `write_history`
  leaves no temp file behind and `read_history` on a missing path is empty.
- `tests/test_verdict.py`: round trip with `gpus`; missing key is empty;
  malformed `gpus` is `None`.
- `tests/test_webapp.py`: `GpuHistoryKeeper` loads from `tmp_path`, records,
  writes and returns averages; a failing writer reports once per distinct
  error and the averages still come back; `collect_snapshot` passes the
  entries and `now` to `history` and stores the result;
  `VerdictPublisher` carries `gpu_averages` into the file.
- `tests/test_ui.py` / `tests/test_app.py`: a card with a matching entry
  renders the row, without one renders `not reported`, no cards renders
  none; the verdict's `gpus` reach the renderer.
- `tests/test_web.py`: the second row appears per card with the same
  fallback.
- `tests/test_gpu.py`: `collect_gpus` fills `card`; the sentinel has none.
