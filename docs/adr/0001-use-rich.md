# 1. Use `rich` for terminal rendering

Date: 2026-08-01

## Status

Accepted

## Context

Harbor Console renders a dashboard to a terminal and redraws it once per second
in place (not by scrolling). We need a rendering approach that can:

- draw a bordered, tabular layout,
- update the full screen each tick without flicker,
- work over a plain TTY / login console with no GUI.

Options considered: raw ANSI escape codes, `curses`, or a higher-level library
such as `rich`.

## Decision

Use `rich`, specifically `rich.table.Table` / `rich.panel.Panel` for layout and
`rich.live.Live` (with `screen=True`) for in-place full-screen refresh.

## Consequences

- `ui.py` stays declarative and free of manual cursor/escape-code handling.
- `Live` owns the redraw loop mechanics; `app.py` only decides *when* to update.
- Adds a third-party dependency (`rich>=13.7`). Acceptable: it is pure-Python,
  widely used, and has no system-level requirements.
- `curses` was rejected as lower-level than needed; raw ANSI was rejected as
  reinventing what `rich` already does reliably.
