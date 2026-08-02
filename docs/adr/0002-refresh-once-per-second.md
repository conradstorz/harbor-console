# 2. Refresh once per second

Date: 2026-08-01

## Status

Accepted

## Context

The dashboard shows live system metrics (CPU, memory, disk, uptime, clock). We
must choose a refresh cadence. Too slow feels stale for an "is my server
healthy?" glance; too fast wastes CPU on a box whose console is always displayed
and provides no additional human-readable value.

## Decision

Refresh the collected metrics once per second (`refresh_interval = 1.0` in
`app.run()`). The `rich` `Live` display runs at `refresh_per_second=4` for smooth
repainting, but new metrics are gathered only once per second.

## Consequences

- The clock advances every second and metrics stay current enough for a glance.
- CPU cost of collection is negligible at 1 Hz.
- The interval is a parameter of `app.run()`, not a config file — it can be
  changed in code or injected by tests, consistent with the "no configuration
  files" constraint (see [ADR 3](0003-no-plugins-in-mvp.md)).
- Sub-second or adaptive refresh is intentionally not implemented (YAGNI).
