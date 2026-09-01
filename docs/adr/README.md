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
| 0005 | [Run as a dedicated `harbor` user, not root](0005-run-as-harbor-user.md) | Accepted |
| 0006 | [Expand scope to a service registry and a tailnet status page](0006-service-registry-and-web-status-page.md) | Accepted |
| 0007 | [Bind the web status page to the Tailscale address only](0007-bind-tailscale-address-only.md) | Accepted |
| 0008 | [Allocate ports rather than validate them](0008-allocate-ports-rather-than-validate.md) | Accepted |
| 0009 | [Write every file atomically, and write `.env` last](0009-atomic-writes-and-env-last.md) | Accepted |

## Adding a new ADR

1. Copy `template.md` to `NNNN-short-title.md` (next zero-padded number).
2. Fill in Context / Decision / Consequences.
3. Set Status to `Accepted` (or `Proposed` if under discussion).
4. Add a row to the table above.
