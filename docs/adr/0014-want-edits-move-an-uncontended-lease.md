# 14. An edited want moves an uncontended lease

Date: 2026-09-09

## Status

Accepted

## Context

`HARBOR_PORTS.md` (template v3, dropped into every participating project by
`ports/explainer.py`) documents "edit `want`, run sync" as the way to change a
port, and promises the operator is moved or told why not: "You may not get
what you asked for -- if another project already holds it, you are moved and
told so."

The allocator's rule 1 (`_decide_one`, the `own is not None` branch)
short-circuited on an existing uncontended lease -- `keep` if the port matches
its `assigned`, `grant` if not -- before `request.want` was ever read. An
operator who edited `want` and ran `sync` saw "up to date" or a routine grant
line, and nothing anywhere said the edit had been ignored. The documented
workflow was a silent no-op.

## Decision

Rule 1's uncontended branch now reads `request.want` before returning. When
`want` differs from the port already held, the wanted key
(`host, addr, want`) is checked for a lease, a same-run promise, and a live
listener, exactly as rule 2's fresh-preference check already does:

- If the wanted key is free, the lease moves to it. The action is
  `"reassign"`, so `sync --new-only` -- the scheduled timer -- withholds it
  the same way it withholds any other reassignment; only a manual `sync`
  renumbers a project.
- If the wanted key is not free, the lease is kept at its current port, and
  the refusal is said out loud: `Decision` gains an optional `note`, carrying
  who or what is blocking the want and which port is being kept instead. The
  CLI (`cli.run`) prepends every decision's `note` to its `warnings` list, so
  it prints as a `warning:` line through the existing mechanism and holds
  `scan` and `sync` to the same non-zero exit compose-default warnings
  already carry.

`want == own.port` or `want` unset are unchanged: the branch falls through to
the existing keep/grant logic exactly as before.

## Consequences

A project moved off its preferred port because that port was held migrates to
it on the first *manual* `sync` after the port frees, which is the documented
semantics of `want` -- and `--new-only` keeps that migration out of automated
runs, same as any other reassignment. "The incumbent always wins" and "a
stopped service keeps its port" are unchanged: a `want` is honoured only when
nothing -- no lease, no same-run promise, no listener -- holds the key it asks
for.

The cost is a warning line (and non-zero exit) an operator was not getting
before, on any tree where a committed `.harbor.toml`'s `want` already
disagrees with its held port and cannot be honoured. That is the intended
behaviour: the note exists precisely to surface a state that was previously
silent.
