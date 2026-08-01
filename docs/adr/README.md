# Architecture Decision Records

This directory records the *why* behind significant decisions, using the
[Michael Nygard ADR format](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions).

Each record is immutable once accepted. When a decision changes, add a new ADR
that supersedes the old one rather than editing history — the point is to keep
the reasoning, including reasoning we later moved away from.

## Records

| #    | Title                                                       | Status   |
|------|-------------------------------------------------------------|----------|
| 0001 | [Use `rich` for terminal rendering](0001-use-rich.md)       | Accepted |
| 0002 | [Refresh once per second](0002-refresh-once-per-second.md)  | Accepted |
| 0003 | [No plugins (and no config) in the MVP](0003-no-plugins-in-mvp.md) | Accepted |
| 0004 | [Run as a systemd service that owns tty1](0004-systemd-tty1-service.md) | Accepted |

## Adding a new ADR

1. Copy `template.md` to `NNNN-short-title.md` (next zero-padded number).
2. Fill in Context / Decision / Consequences.
3. Set Status to `Accepted` (or `Proposed` if under discussion).
4. Add a row to the table above.
