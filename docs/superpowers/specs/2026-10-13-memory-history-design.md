# Memory history: a week of low-memory episodes, stalls and OOM kills

Date: 2026-10-13

## Goal

Answer "has this host been short of memory, when, and did it cost anything?"
for the last hour, 3 hours, 7 hours, day and week. Three signals, one
timeline:

- **Low episodes** -- runs of samples where available memory was under 10%
  of total.
- **Stall time** -- seconds tasks spent waiting on memory, from the kernel's
  pressure-stall counters (PSI), `some` and `full`.
- **OOM kills** -- how many processes the kernel's OOM killer killed.

The status page shows the full picture; the console gets one line. This is
the project's second persistent file, so it gets ADR 23 (ADR 22 says a
second file needs one).

## Approach

The same shape as the GPU busy history (ADR 22): the web prober samples
and persists, the console only reads the verdict file.

The prober wakes about every 30 s. A threshold sampled that often misses a
dip that comes and goes between two samples, which is the case that matters
most -- a burst that ends in an OOM kill. The two counters close that gap:
PSI `total=` and `/proc/vmstat oom_kill` are cumulative since boot, so the
delta between two samples counts everything that happened between them, at
whatever speed it happened. The threshold episodes are the coarse signal;
the counters are the ones that cannot miss.

Raw samples are kept, not derived events, so the rules (threshold, gap,
episode boundaries) can change later without losing the history.

Rejected: an event-only log (smaller, but fixes the rules at write time and
loses the minimum-available trend); merging into `gpu-history.json` (couples
two codecs and supersedes ADR 22 instead of extending it).

## Components and data flow

- `memory_pressure.py` (new) -- **collects**. Reads files only, no
  subprocess, nothing that can hang. `collect_memory_reading(read_text=...)
  -> MemoryReading` with every field optional, each file and field guarded
  on its own, never raises:
  - `available_percent: float | None` -- `MemAvailable / MemTotal * 100`
    from `/proc/meminfo`.
  - `stall_some_us: int | None`, `stall_full_us: int | None` -- the
    `total=` values of the `some` and `full` lines of
    `/proc/pressure/memory`. Absent on a kernel without PSI.
  - `oom_kills: int | None` -- `oom_kill` from `/proc/vmstat`.
  - `boot: int | None` -- `btime` from `/proc/stat`, epoch seconds; how a
    reset counter is told apart from a quiet one.
  `read_text` is injectable (defaults to `Path.read_text`), so tests feed
  file contents as strings.
- `memory_history.py` (new) -- pure policy plus codec, in the shape of
  `gpu_history.py`, pure in `now`:
  - `Sample(at: datetime, available_percent: float | None, stall_some_us:
    int | None, stall_full_us: int | None, oom_kills: int | None, boot: int
    | None)`.
  - `History = tuple[Sample, ...]`, oldest first.
  - `record(history, reading, now) -> History`: appends one sample at `now`
    (always -- a sample with every field `None` is still a cycle that
    looked), drops samples older than `RETENTION` (7 d). Never mutates.
  - `MemoryWindow(window, seconds, min_available: float | None,
    low_episodes: int, stall_some_seconds: float | None,
    stall_full_seconds: float | None, oom_kills: int | None,
    covered_seconds: int)`.
  - `MemoryEvent(kind: "low" | "oom", at: datetime, duration_seconds: int,
    min_available: float | None, count: int)`.
  - `MemorySummary(windows: tuple[MemoryWindow, ...], events:
    tuple[MemoryEvent, ...])`.
  - `summarize(history, now) -> MemorySummary`: the windows are
    `gpu_history.WINDOWS`, imported, so both histories cut time the same
    way.
  - `format_console_line(summary, now) -> str`, `format_duration`, see
    Rendering.
  - `dumps`, `loads` (never raises; garbage is an empty history),
    `read_history` (never raises), `write_history` (atomic temp file then
    `os.replace`; raises `OSError`, the caller reports).
- `webapp.py` -- `GpuHistoryKeeper` becomes `HistoryKeeper`, parametrised
  by its `record` and `summarize` functions, its reader and writer, so the
  GPU and memory histories share one keeper: load once in `__init__`,
  record/write/summarize per call, a failed write reported once per distinct
  error and never raised, the in-memory history still advancing.
  `collect_snapshot` gains `memory_history: Callable[[MemoryReading,
  datetime], MemorySummary | None]` (default returns `None`) and
  `memory_reader` (default `collect_memory_reading`). `_default_prober`
  wires both keepers.
- `snapshot.py` -- gains `memory_summary: MemorySummary | None = None`.
- `verdict.py` -- `Verdict` gains `memory: MemorySummary | None = None`,
  serialised with the windows and events. A missing `memory` key loads as
  `None` (old writer, new reader across a deploy); a present but malformed
  one makes the whole verdict `None`, as for `gpus`. `VerdictPublisher`
  copies `snapshot.memory_summary` in.
- `app.py` / `ui.py` -- `build_dashboard` gains `memory_summary:
  MemorySummary | None = None`; `app.run` passes the verdict's `memory`.
- `web.py` -- a memory-events section, see Rendering.

The console keeps zero writes and zero new reads.

## The history file

- Path: `/var/lib/harbor-console/memory-history.json`, under the
  `StateDirectory` ADR 22 already created. `HISTORY_PATH` is a constant in
  `memory_history.py`; no flag moves it.
- Shape: `{"samples": [[epoch, available_percent, some_us, full_us,
  oom_kills, boot], ...]}`, oldest first, `null` for a field that was not
  read. `available_percent` rounded to one decimal.
- One sample per prober cycle; 7 d is about 20k samples, under 1 MB of
  JSON, rewritten atomically every cycle (about 3 GB/day; negligible on an
  SSD, and simpler than batching -- the same trade ADR 22 accepted).
- Loaded once at startup; missing, unreadable or malformed is an empty
  history, unreported.
- `uninstall.sh --purge` already removes `/var/lib/harbor-console`.

## Rules

Constants in `memory_history.py`, no configuration:

- `LOW_AVAILABLE_PERCENT = 10.0`. A sample is low when `available_percent <
  10.0`. A sample with `available_percent` `None` is not low and breaks a
  run: absence of evidence is never a finding.
- `MAX_SAMPLE_GAP = 120 s`. Two low samples belong to one episode when they
  are consecutive in the history and no more than 120 s apart (the prober
  sleeps 30 s *after* a cycle whose probes can take seconds, so the gap is
  more than 30 s and varies). A longer gap -- the prober or host was down --
  ends the episode; nobody saw what happened in between.
- An episode's `at` is its first sample, `duration_seconds` its last minus
  its first (0 for a single sample), `min_available` the lowest value in it.
  It counts toward a window when any of its samples is inside the window.
- **Counter deltas.** For each pair of consecutive samples where both have
  the counter: if both have `boot` and the boots differ, or the counter went
  down, the host rebooted and the delta is the later value (counted from
  zero since boot); otherwise it is later minus earlier. The delta is
  credited at the later sample's time, and counts toward a window when that
  time is inside it. A window with no usable pair has `None` for that
  counter -- unknown, not zero.
- An `oom` event is one credited delta greater than zero: `at` the later
  sample, `count` the delta.
- `events`: the five most recent `low` and `oom` events in the 7 d window,
  newest first.
- `min_available`: the lowest `available_percent` in the window, `None` when
  there is none.
- Coverage: `covered_seconds` is `now` minus the oldest sample in the
  window, as in ADR 22, and is flagged below 95% of the window.
- `now` is `snapshot.collected`, injected.

## Rendering

- `format_duration(seconds)`: `45s` under a minute, whole minutes under an
  hour (`12m`), whole hours under a day (`5h`), days otherwise (`2d`),
  `floor`ed. Shared by stall time, episode length, coverage and age.
- **Console**, one row under Swap, labelled `Mem events`. Value from the
  verdict's 7 d window and its newest event:
  `7d: 3 low · last 2h ago · stall 12s · oom 1`
  - `7d (2d):` when the 7 d window is under 95% covered.
  - `last … ago` only when there is an event; age is `now` minus the event's
    `at`, formatted with `format_duration`. Omitted otherwise.
  - `stall` is the `full` stall time -- the one where every task waited.
    `stall —` / `oom —` when the counter is unknown.
  - `not reported` when there is no verdict or it has no `memory`.
    Staleness stays the banner's job.
  - Width: the fullest plausible row (`7d (6d): 99 low · last 23h ago ·
    stall 59m · oom 99`, 51 cells) is pinned by a test to the 54-cell value
    width.
  - Uses one more of ADR 20's spare rows; ADR 23 records it.
- **Web page**, a `Memory events` section after the memory rows:
  - A table, one row per window: `window`, `min avail`, `low`, `stall
    some`, `stall full`, `oom`, with the coverage flag beside the window
    name as on the GPU row. `—` for `None`.
  - Below it, the recent events, one line each: `2026-10-13 14:02 low 12m,
    min 6.2%` or `2026-10-13 14:14 oom ×1`. `No low-memory events this week.`
    when there are none.
  - `Not reported yet.` when `snapshot.memory_summary` is `None`.
- No colour, no banner. An OOM kill is a workload event, not a platform
  fault (ADR 21).

## Deployment

- No unit directive changes: the `StateDirectory` exists, and
  `ProtectSystem`/`ProtectHome` leave `/proc` readable. The comment above
  `StateDirectory=` in `harbor-console-web.service` and the purge message in
  `uninstall.sh` name both histories instead of only the GPU one.
- ADR 23, "Keep a week of memory pressure history beside the GPU history":
  the second persistent file, why sampled threshold plus cumulative counters,
  the 10% constant, the console row.
- `CLAUDE.md`: `memory_pressure.py` and `memory_history.py` in the module
  list; the single-writer rule and the scope paragraph name both history
  files.

## Testing

Plain values, fixed datetimes, no real time, sockets or disk except
`tmp_path`.

- `tests/test_memory_pressure.py`: each file parsed from strings; each file
  missing or garbled leaves only its own fields `None`; nothing raises.
- `tests/test_memory_history.py`: `record` appends, prunes, never mutates;
  episodes split by threshold, by a `None` sample and by a gap over 120 s;
  counter deltas for a steady counter, a rise, a decrease (reboot) and a
  boot change; a window with no usable pair is `None`; events ordering and
  the cap of five; coverage; `format_console_line` for no events, events,
  unknown counters, the flagged 7 d window and the width-pinned fullest row;
  codec round trip and `loads` on garbage.
- `tests/test_verdict.py`: round trip with `memory`; missing key is `None`;
  malformed is `None` for the whole verdict.
- `tests/test_webapp.py`: `HistoryKeeper` for both histories (load, record,
  write, failed write reported once and summary still returned);
  `collect_snapshot` passes the reading and `now` and stores the summary;
  `VerdictPublisher` carries it.
- `tests/test_ui.py` / `tests/test_app.py`: the row renders from a summary,
  `not reported` without one; the verdict's `memory` reaches the renderer.
- `tests/test_web.py`: the table, the events list, the empty and
  not-reported cases.
