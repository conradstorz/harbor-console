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
| 0008 | [Allocate ports rather than validate them](0008-allocate-ports-rather-than-validate.md) | Superseded by 0015 |
| 0009 | [Write every file atomically, and write `.env` last](0009-atomic-writes-and-env-last.md) | Superseded by 0015 |
| 0010 | [Scope the port key to the bind address: `(host, addr, port)`](0010-address-scoped-port-key.md) | Superseded by 0015 |
| 0011 | [`sync` repairs drifted projects, and `show` stands alone](0011-sync-repairs-drift-and-show-stands-alone.md) | Superseded by 0015 |
| 0012 | [The web surface collects by convention, not by declaration](0012-web-surface-collectors-and-conventions.md) | Accepted |
| 0013 | [The ledger stays in the repository](0013-ledger-lives-in-repo.md) | Superseded by 0015 |
| 0014 | [An edited want moves an uncontended lease](0014-want-edits-move-an-uncontended-lease.md) | Superseded by 0015 |
| 0015 | [A reverse proxy fronts every HTTP service; labels are the only declaration](0015-reverse-proxy-and-label-declared-services.md) | Accepted |

## Adding a new ADR

1. Copy `template.md` to `NNNN-short-title.md` (next zero-padded number).
2. Fill in Context / Decision / Consequences.
3. Set Status to `Accepted` (or `Proposed` if under discussion).
4. Add a row to the table above.
