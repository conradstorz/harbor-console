# 11. `sync` repairs drifted projects, and `show` stands alone

Date: 2026-09-01

## Status

Accepted — amends [ADR 8](0008-allocate-ports-rather-than-validate.md), which
described `sync` as applying decisions, and adds two obligations to it.

## Context

[ADR 8](0008-allocate-ports-rather-than-validate.md) made a project's `.env` the
thing that actually binds the port; the lease is only the record of who is
entitled to it. Implementation found two ways that arrangement fails while
everything reports success.

The first is the fresh clone. `.env` is gitignored, so *every* new checkout of a
participating project starts without one, while its lease stands and its
allocation decision is "keep". A run that wrote only projects whose decision had
changed would print "up to date" over a project about to interpolate its compose
default — a number nobody has checked, on a port that may be leased to somebody
else. That is this tool's founding failure reached at exit 0. The same applies to
a deleted or hand-mangled fence, and to a missing `HARBOR_PORTS.md`, which is the
only thing telling the next person why the number is there at all.

The second is a broken declaration blocking a read. Loading declarations means
parsing a `.harbor.toml` in every participating repository, any one of which may
be mid-edit or malformed. When that made *every* subcommand fail, `show` — the
one command that only reads this repository's own ledger — went down with them,
precisely when an operator investigating the breakage needs the lease table.

## Decision

We will make `sync` responsible for the *state* of a participating project, not
only for its *changes*. A project whose `.env` fence or `HARBOR_PORTS.md`
disagrees with the lease it already holds is written, even though nothing was
granted or moved. `scan` reports the same condition without writing. A repair is
reported in its own words, distinctly from a grant, because restoring a lease a
project already holds is not the same event as handing it a new port. A tree that
already matches stays a genuine no-op: no file is opened for writing and the
ledger's mtime does not move.

We will make `show` independent of declaration loading. It reads the ledger and
prints it; it parses no `.harbor.toml` at all.

## Consequences

- A fresh clone is repaired on the next `sync` instead of silently running on its
  compose default. The lease and the file that honours it converge.
- `sync` now touches projects the operator did not change, so the report must
  distinguish repairs from grants or it reads as unexplained churn. It does.
- A broken declaration anywhere in the tree still fails `scan` and `sync` — those
  cannot allocate against data they cannot read — but no longer hides the lease
  table. `show` keeps working during exactly the incident that breaks the others.
- Repair widens the set of files opened per run, so the pre-write validation of a
  project's `.env` fence covers repaired projects too: a corrupted fence is never
  guessed at, and a project whose `.env` cannot be read is dropped whole and
  named.
- A compose default that disagrees with the assignment is *not* repairable this
  way — harbor-console does not edit compose files. It stays a warning and a
  non-zero exit until the project's owner updates the default, which is why
  `HARBOR_PORTS.md` has to say so.
