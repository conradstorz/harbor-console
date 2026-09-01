# 10. Scope the port key to the bind address: `(host, addr, port)`

Date: 2026-09-01

## Status

Accepted — supersedes the `(host, port)` uniqueness rule of
[ADR 6](0006-service-registry-and-web-status-page.md), and amends
[ADR 8](0008-allocate-ports-rather-than-validate.md), which decided allocation
without saying what a port number is scoped to.

## Context

[ADR 6](0006-service-registry-and-web-status-page.md) made `(host, port)` unique
in the registry, and `founding_document.txt` repeated it in the v0.2.0 release
criteria. That rule was written for a registry of one service per port per
machine, before anything had to allocate against real host state.

Implementation met a case it cannot describe. ARM publishes
`100.69.239.123:49152:8080` — a port bound to one specific tailnet address on
hpz440, not to every interface. Under `(host, port)` that claim reserves 49152
across the whole host, so no other project could ever be granted 49152 on that
machine even on a different address, and any second address-scoped publish of
the same number would be a duplicate and a hard load-time error. Neither is
true of the operating system: two listeners can hold the same port on two
different addresses.

The reverse mistake is worse. A publish on `0.0.0.0` really does claim the port
on every address of that host, so treating an address-scoped claim and a
wildcard claim as unrelated would let the allocator hand out a port that is
already taken — the exact failure this project exists to prevent.

The two facts are asymmetric, so the key cannot simply gain a field and be
compared for equality. Overlap, not equality, is the relation that matters.

## Decision

We will make the uniqueness key **`(host, addr, port)`**, and compare addresses
by *overlap* rather than equality. `ports/keys.py` owns that one rule:

- `0.0.0.0` is the wildcard. It overlaps every address on its host, in both
  directions.
- Two different specific addresses on the same host do not overlap. Each may
  hold the same port number.
- Two different hosts never contend, whatever their addresses.

The ledger carries `addr` as a first-class field on every lease, and rejects any
self-contradiction under that rule when the file loads — a hard error, not a
warning, exactly as ADR 6 intended for the narrower key. The allocator and the
live-state check (`LiveState.is_listening`, `container_on`) use the same
function, so "is this port free?" is answered one way everywhere.

## Consequences

- ARM keeps `100.69.239.123:49152` without claiming 49152 from every other
  project on hpz440. Address-scoped publishing is a legitimate, representable
  claim rather than something the ledger has to lie about.
- A wildcard claim still blocks everything on its host, so the 8080 collision
  stays caught. Nothing is loosened in the direction that would let two projects
  onto one socket.
- Grandfathering an already-running service records the address it is actually
  bound to, which is what makes adoption of the running fleet lossless.
- The ledger gains a field. `services.toml` entries are
  `project, name, host, addr, port, granted` — a duplicate `(host, port)` on
  two distinct addresses is now valid data, so any tooling that assumed port
  numbers are unique per host must be corrected rather than trusted.
- Overlap is not an equivalence relation: `0.0.0.0` overlaps `10.0.0.1` and
  `10.0.0.2`, which do not overlap each other. Detection is therefore pairwise,
  and cannot be done by hashing a key into a set. The ledger is small enough
  that the quadratic check costs nothing.
- IPv6 and CIDR are out of scope. Only exact addresses and the single `0.0.0.0`
  wildcard are understood; anything broader needs its own record.
