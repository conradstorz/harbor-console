# 23. Keep a week of memory pressure history beside the GPU history

Date: 2026-10-13

## Status

Accepted

## Context

The Memory row shows the instant: used, total, percent. The question it
cannot answer is whether the host ran short of memory while nobody was
looking -- and when it does run short, it is usually for seconds, ending in
the kernel killing something. A 30 s sample of available memory is the
wrong instrument for that on its own: a burst that comes and goes between
two samples is invisible to it.

The kernel keeps two counters that are the right instrument. PSI's
`/proc/pressure/memory` `total=` is the cumulative time tasks spent stalled
on memory, and `/proc/vmstat` `oom_kill` the cumulative OOM kills, both
since boot. The difference between two readings counts everything between
them, however brief.

ADR 22 made the GPU busy history the project's one persistent file and
said a second needs its own ADR.

## Decision

We will keep a week of memory samples in
`/var/lib/harbor-console/memory-history.json`, beside the GPU history, with
the same single writer (the web prober), the same atomic rewrite, the same
windows and the same 95% coverage rule.

Each sample carries available percent, the PSI `some` and `full` totals,
the OOM kill counter, and `btime`. From them the prober derives, per
window: the minimum available percent; low episodes, runs of samples under
10% available no more than 120 s apart; stall seconds and OOM kills from
counter deltas, where a counter that went down or a boot time that moved
more than 60 s is a reboot. It keeps the five most recent events.

Raw samples are stored, not events, so these rules can change without
losing the week. The 10% line is a constant; there is no configuration.

The page shows a table per window and the recent events. The console shows
one line under Swap from the verdict file it already reads:
`7d: 3 low · last 2h ago · stall 12s · oom 1`. No colour and no banner: an
OOM kill is a workload event, not a platform fault (ADR 21).

## Consequences

- The project keeps two files across restarts. A third needs a new ADR.
- The verdict file carries checks, GPU averages and the memory summary.
- The verdict decoder is strict about the memory summary: the exact window
  set, finite numbers, and naive, ordered event times. A malformed summary
  is dropped on its own -- the console line says `not reported` -- while the
  checks and the GPU averages still load, rather than crashing the console,
  showing a number nobody can trust, or taking the platform banner with it.
  The writer clamps what it emits (available memory to 0-100, durations and
  coverage to non-negative) so it cannot produce a summary its reader rejects.
- About 20k samples a week, about 1 MB, rewritten every cycle: about 3 GB
  a day, negligible on an SSD, accepted for the same reason as ADR 22.
- After a prober outage without a reboot, the stall and OOM deltas that
  accumulated during the outage are credited at the first sample after the
  restart, so a short window can over-count, and an event's time is when it
  was seen, not when it happened.
- The console spends one more of ADR 20's three spare rows. With one GPU
  on hpz440 that leaves one; while the banner is up the panel's bottom is
  cropped further, and ADR 20's remedy (a smaller font) still applies.
- A kernel without PSI shows `—` for stall time and still reports episodes
  and kills. Unknown is never zero.
- Sampling still misses a dip under 10% that recovers between samples; the
  stall and kill counters are what catch those, which is why both exist.
- Rejected: an event-only log (fixes the rules at write time and loses the
  minimum trend); one combined history file (couples two codecs and would
  supersede ADR 22 rather than extend it).
