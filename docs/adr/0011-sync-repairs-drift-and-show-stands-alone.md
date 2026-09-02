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

A repair restores a **lease**, and only a lease. It is bounded by three rules,
because a repair writes into a repository this tool does not own:

- A project holding no lease has nothing to repair. An `assigned` in somebody
  else's `.harbor.toml` is not a reservation — it is a committed number that
  stays there after the port has been granted away — so it is never treated as
  "the port this project is already on".
- A port whose reassignment `--new-only` withheld publishes the number its lease
  says, or nothing when it holds no lease. A run that has just refused to
  renumber a port may not renumber it by another route — but publishing the
  number a project already holds is not renumbering it, and is the whole point
  of the repair pass, so a withheld port with a lease and no `.env` behind it is
  repaired to that lease whether it is one port of several or the only port the
  project has. The bound is on the *port*, not the project around it: refusing
  to move one port is not a reason to leave a second one unpublished on the
  unattended path.
- A repair is drift this run's own writes do not already account for. A grant
  writes the variable it grants, so adding a port to a project in perfect sync
  is reported as a grant and nothing else; drift is judged against the variables
  the run is *not* writing. `scan` and `sync` compute the set through one
  predicate, each discounting the ports its own write pass lands, so `scan`
  predicts `sync` exactly. It predicts `sync --new-only` too, except where that
  command withholds a move: the port `scan` shows as a grant is the one
  `--new-only` reports withheld and repairs to its standing lease.
- Only the files that actually disagree are written. Being repaired for one file
  does not rewrite the others.

We will make `show` independent of declaration loading. It reads the ledger and
prints it; it parses no `.harbor.toml` at all.

## Consequences

- A fresh clone is repaired on the next `sync` instead of silently running on its
  compose default. The lease and the file that honours it converge.
- `sync` now touches projects the operator did not change, so the report must
  distinguish repairs from grants or it reads as unexplained churn. It does.
- A repair is a write on the unattended path, so its bounds are load-bearing —
  and being load-bearing, they have to be exactly as wide as the harm. Repairing
  a project that held no lease published the incumbent's port into it — two
  projects on one port, reached through the mechanism meant to prevent that, at
  exit 0 and reported as a repair. What stops that is the first rule: no lease,
  nothing published. Bounding by the whole project instead stopped it a second
  time and cost the case above, where the same run left a genuine lease with no
  `.env` behind it and said nothing. Bounding by port and then judging drift
  against the whole fence cost the opposite: every grant to a project holding
  any other port printed a repair beside it, and the warning that a sibling
  clone is running on its compose default became a line an operator skims.
- A project can therefore be reported as both granted and repaired in one run:
  one port moved, and others it already held came back. That is two events, and
  the report names them separately rather than folding the second into the
  first, where an operator would never see it.
- Bumping the `HARBOR_PORTS.md` template version puts every participating
  project into the repair set at once. Writing only the files that differ is
  what keeps that from being an mtime change on every project's `.env`.
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
