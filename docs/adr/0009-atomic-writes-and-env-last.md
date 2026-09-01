# 9. Write every file atomically, and write `.env` last

Date: 2026-09-01

## Status

Accepted

## Context

[ADR 8](0008-allocate-ports-rather-than-validate.md) decided that the allocator
writes into repositories harbor-console does not own. Three files per project:
`HARBOR_PORTS.md`, `.harbor.toml`, and `.env` — plus this repository's own
`services.toml` ledger. That decision put two failure modes in reach that a
read-only tool never had, and both of them destroy something.

The first is the partial write. `Path.write_text` truncates the target when it
opens it, so a write that fails partway — a full disk, a revoked permission, an
I/O error, a `KeyboardInterrupt` — leaves somebody else's `.env` replaced by
nothing. That file holds secrets, is routinely gitignored, and is therefore not
in anyone's version control and not in any backup this tool can appeal to. There
is no undo.

The second is the ordering of a project's three writes against the ledger. A
project whose write fails has *this run's* decisions withdrawn from the ledger,
so the port it was being granted goes back to being free. `.env` is the file
that actually makes the container bind the port; the ledger is only the record
of who is entitled to it. If `.env` were written and something later failed, the
withdrawal would be a lie: that project would be publishing a port the ledger no
longer reserves, and the allocator would hand the same port to the next
requester. Two projects on one port is the exact failure this whole system
exists to prevent, and it would have been reached through the mechanism built to
prevent it. It does not heal itself either — only a lease reserves a port; an
`assigned` value in a `.harbor.toml` does not.

## Decision

We will make **every whole-file write atomic**. `ports/atomic.py` writes the new
content to a temporary file in the *same directory* as the target and then moves
it over with `os.replace`, which is atomic on POSIX and on Windows. Every writer
in the allocator — the ledger, `.harbor.toml`, `.env`, `HARBOR_PORTS.md` — goes
through it. A reader sees either the whole old file or the whole new one, and
any failure before the replace leaves the original exactly as it was.

Within one project, we will write in a fixed order: **`HARBOR_PORTS.md`, then
`.harbor.toml`, then `.env` last**. `.env` is written last precisely so that
every failure before it leaves the ledger withdrawal truthful — nothing
publishes a port the ledger does not grant.

## Consequences

- A failed write costs a re-run, never a file. The tool cannot empty another
  repository's `.env`.
- Temp files are named `.harbor-tmp.<target name>.<random>.tmp`, in the target's
  directory, so one `.gitignore` rule — `.harbor-tmp.*` — sweeps every possible
  leftover regardless of which file was being written. A rule for `.env` would
  not have matched a temp file derived from it, and an abandoned temp file may
  contain secrets in full. Participating projects need that rule too; it is
  documented for them.
- Same-directory temp files are required, not a preference: `os.replace` is not
  atomic across filesystems and on Windows does not work across them at all, and
  the system temp directory is routinely on a different one. A writer that
  cannot create a file beside its target fails rather than falling back.
- **Accepted residual: no `fsync` before the replace.** This covers *failed
  writes*, not power loss. After a crash or a power cut the filesystem may have
  the replace without the data behind it, and a leftover temp file may remain.
  `fsync` on every write was judged disproportionate for a tool that writes a
  handful of small files by hand or on an hourly timer; the sweep pattern and a
  re-run cover what is left.
- The write order is load-bearing, not stylistic. Reversing it — writing `.env`
  before the declaration or the explainer — reintroduces the collision this
  project was built to stop, so it needs an ADR that supersedes this one.
