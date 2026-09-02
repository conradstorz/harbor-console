# 12. The web surface collects by convention, not by declaration

Date: 2026-09-02

## Status

Accepted

## Context

`harbor-console-web` must probe each declared service and reconcile the ledger
against Docker. But the server holds only `services.toml`, deployed by
`install.sh`. The projects' `.harbor.toml` files — which carry `container`,
`health_path`, `hcstatus_path` and `description` — live on the dev box and are
never deployed. The prober therefore has a lease and nothing else.

Three ways out were considered: copy the descriptive fields into each lease
record; emit a second generated directory file alongside the ledger; or probe by
convention and reconcile on the key the ledger already owns.

Two facts made the third sufficient. Liveness is deliberately dumb — any HTTP
response means up — so a declared health path barely affects up or down; even a
404 proves a service is answering. And `(host, addr, port)` is already the
ledger's key, so it is enough to join leases against Docker's published ports
without knowing any container's name.

## Decision

We will probe `/` for liveness and `/hcstatus` for optional detail, by
convention, on every leased port. We will reconcile by joining leases against
Docker's published ports on `(addr, port)`, compared by address overlap.

Nothing new is deployed and no descriptive field is copied, so no field can go
stale against the declaration that owns it.

Because the ledger carries no container name, a port mismatch is reported only
when a container's name equals a lease's project name. An unmatched pair is
reported as what it literally is — declared-not-running plus
running-not-declared, the same event seen from both sides.

Sockets are enumerated with `psutil.net_connections`, which is already a
dependency and sees loopback-bound and non-Docker listeners that Docker cannot
report. A socket bound to IPv6 `::` is normalised to `0.0.0.0`, because it
accepts IPv4 traffic and is the wildcard in practice.

## Consequences

- The page needs no deployment artifact beyond the ledger it already has.
- A project cannot declare a custom health path and have the page honour it. If
  one ever genuinely needs to, that is a new decision and a new ADR.
- Name-based mismatch detection is coarse: `arm-rippers-dev` will not be matched
  to `automatic-ripping-machine`, and that pair reports as two findings rather
  than one. Adding a `container` field to the lease would fix it and needs an
  ADR of its own.
- The allocator's fourth drift category — a project's `.env` disagreeing with
  its lease — remains invisible to the page, because it needs files the server
  does not have. `ports scan` reports it; the page cannot and does not pretend
  to.
