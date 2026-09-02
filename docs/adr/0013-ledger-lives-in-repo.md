# 13. The ledger stays in the repository

Date: 2026-09-02

## Status

Accepted

## Context

Since [ADR 6](0006-service-registry-and-web-status-page.md), where the ledger
lives on disk has been listed as deliberately undecided: in-repo and deployed
to `/opt/harbor-console/` by `deploy/install.sh`, or a path under `/etc`. It
stayed in-repo only because that is where `ports sync` runs, not because the
question was settled. [ADR 8](0008-allocate-ports-rather-than-validate.md) has
since answered the sibling question — how the port authority is enforced, by
allocation — leaving only this one open.

The deciding fact is *who writes the ledger*. `save_leases` is called only
from `ports/cli.py`, which runs on the dev box; hpz440 has no checkout of the
project tree, and `webapp.py` only ever reads `services.toml`. So nothing on
the server writes it, ever. `deploy/install.sh`'s `rsync -a --delete` is not
clobbering server-side state that the server itself produced — it *is* the
deployment, the one and only way a fresh copy reaches the server at all.

`rsync -a` writes to a temp file beside the target and renames into place, so
a re-run mid-cycle cannot produce a torn read of `services.toml` on the server.
`--delete` removes files on the destination that no longer exist on the
source, but excluded paths are protected from deletion unless
`--delete-excluded` is also passed — `install.sh` does not pass it — so
`/opt/harbor-console/.venv` is untouched by the same sync that refreshes the
ledger.

The `/etc` alternative was weighed and rejected on the same fact: the
allocator writes on the dev box regardless of where the served copy lives, so
an `/etc` copy still needs the same rsync to get there. That is identical
staleness under a different path, plus a copy that no longer lives in git. The
ledger is a plain file rather than a database precisely so it is diffable and
reviewable in the repository's own history; moving the served copy to `/etc`
gives that up for nothing in return.

## Decision

We will keep `services.toml` in the repository, deployed to
`/opt/harbor-console/` by `deploy/install.sh`'s `rsync -a --delete`, exactly as
it is today. This closes the question ADR 6 left open. No `/etc` copy, and no
change to where `ports sync` writes.

## Consequences

- The open question in `founding_document.txt` and `CLAUDE.md` is closed; both
  now point here instead of listing it as undecided.
- The residual is staleness, not loss. Run `ports sync` and forget
  `install.sh`, and the server goes on serving an old ledger until the next
  deploy. That degrades the page's directory and drift section — a project
  that took a new lease since the last deploy is missing from both.
- It does **not** affect the allocator itself: `/ports.json`, the payload the
  allocator actually reads, is built from listening sockets and Docker on the
  server, never from the ledger the server holds. A stale served ledger cannot
  cause a bad grant.
- The one sharp edge is `harbor-console-web` itself: it takes its own port
  from its own lease in the ledger at startup (`webapp.own_lease`), so a stale
  copy plus a lease that was reassigned since the last deploy would have the
  service try to bind the old port on its next restart. This is unlikely in
  practice — nothing renumbers an incumbent lease — but it is real, and it is
  the one case where staleness reaches past the display and into behavior.
- To make the display half of that residual legible without logging in, the
  status page now shows when the ledger it is serving was last written,
  beside the existing "Collected" timestamp in the page footer. `Snapshot`
  carries a `ledger_written: datetime | None` field, populated in
  `webapp.collect_snapshot` by the injectable `ledger_mtime` collector
  (`webapp.read_ledger_mtime`, reading the ledger file's mtime, defaulting to
  `webapp.LEDGER_PATH`). Like every other collector in this project it
  degrades rather than raises: a missing or unreadable ledger file yields
  `None`, and the page renders "services.toml written unknown" rather than a
  bare `None` or a crash. An operator who ran `ports sync` and forgot
  `install.sh` now sees a footer timestamp that stopped moving, instead of
  discovering the gap only when a service they expected to see is missing
  from the directory.
