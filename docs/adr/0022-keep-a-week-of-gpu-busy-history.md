# 22. Keep a week of GPU busy history in the web unit's state directory

Date: 2026-10-09

## Status

Accepted

## Context

The GPU row on both surfaces shows the instant: busy percent, VRAM, heat,
power, fan, read fresh each cycle. The question the row cannot answer is
whether the card was busy over the last hour, the last day, the last week
-- the one an operator sizing an inference server actually asks, and the
one the instant reading is worst at, since it is sampled once a second on
tty1 and once every 30 s on the page from a load that swings between idle
and saturated in under a minute.

An average over a week needs a week of samples, and nothing in this project
keeps anything. The founding document deferred persistence and every
module has honoured that: the console restarts on every tty1 login, the
web prober on every deploy, and the one file that crosses between them
lives in `/run`, which a reboot wipes (ADR 21). A week of history cannot
come from any process's memory.

## Decision

We will keep a week of GPU busy samples on disk, written by the web prober
and nobody else, and show five averages -- 1 h, 3 h, 7 h, 24 h, 7 d --
under each GPU row on both surfaces.

The prober appends one busy sample per card per cycle to
`/var/lib/harbor-console/gpu-history.json`, under a systemd
`StateDirectory` owned by `harbor`, prunes everything older than a week,
rewrites the file atomically, and puts the five averages into the verdict
file beside the checks. The console reads them from the file it already
reads once per tick (ADR 21) and computes nothing: zero writes, zero new
reads, nothing that can block the 1 Hz loop.

A window the samples cover less than 95% of says so beside its mean
(`7d 18% (2d)`), and a window with no samples shows a dash. Absence of
evidence is never a number (ADR 18).

Busy percent only. The other GPU metrics stay instantaneous.

## Consequences

- Both rows under a GPU say what the card has been doing, not only what it
  is doing this second.
- The project keeps one file across restarts and reboots. ADR 21's "the
  verdict file carries checks" widens to "checks and the GPU averages";
  the founding document's "no persistence" narrows to "no persistence but
  this file". A second persistent file needs a new ADR.
- The prober rewrites about 400 KB every 30 s, about 1 GB a day. Negligible
  on an SSD; accepted over batching writes, which would lose samples on a
  crash for no operational gain.
- The console spends one of its three spare rows per card (ADR 20). While
  the platform banner is up it takes all three, so the panel's bottom
  border is cropped; ADR 20's remedy, a smaller console font, applies and
  is not taken here. During the first day of a fresh history the row wraps
  once; it stops on its own.
- A lost or corrupt history file costs the week it held and nothing else:
  the keeper starts empty and the windows say how little they cover.
- Rejected: a shared history both processes write (two writers, a lock, and
  a disk write per second in the console); in-memory history (no process
  lives a week); downsampling (20k integers a week is small, and a plain
  mean over plain samples is the rule anyone can check by hand).
