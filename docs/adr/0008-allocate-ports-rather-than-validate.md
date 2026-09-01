# 8. Allocate ports rather than validate them

Date: 2026-09-01

## Status

Accepted

## Context

[ADR 6](0006-service-registry-and-web-status-page.md) added the registry but
deliberately left open what makes it *binding*: a file nobody consults is not an
authority. Three mechanisms were on the table — an advisory check command, the
drift list on the status page as the only enforcement, or generated
configuration each project reads.

Two facts decided it. Ports are claimed on the Windows dev box, where the
project tree and the compose files live, but observed on hpz440, where the
containers run; deploys go out through a remote Docker context. And this
repository has no CI, so "fail it in the pipeline" is not an available venue.
An advisory check is only as good as remembering to run it, and the drift list
alone reports a collision that has already happened.

## Decision

We will make the registry binding by having it **hand out the number**, not
merely judge one.

A project declares what it needs in `.harbor.toml`. `harbor-console ports sync`
allocates a free port from 8100–8999, records a lease in `services.toml`, and
writes the number into the project's own `.env` behind a managed fence. Compose
consumes it as `"${HARBOR_PORT_NAME:-default}:container"`, so the project still
starts when harbor-console has never run — the interpolation default is the
project's own preference.

Conflicts are resolved by lease date: the incumbent keeps the port and the
newcomer is moved. Nothing running is ever renumbered. A port is free only when
it is neither leased nor listening, so a stopped service keeps what it holds.

An unattended run may grant new requests but never change an existing
assignment.

## Consequences

- The registry is consulted by construction, because it is the thing that
  produces the number.
- harbor-console writes into repositories it does not own — `.harbor.toml`,
  `.env` and `HARBOR_PORTS.md`. This is the coupling ADR 6 declined to choose,
  now chosen deliberately. Each participating project also needs a one-line
  compose change.
- Every participating project carries `HARBOR_PORTS.md`, identical everywhere,
  so the rules are legible from inside a repository that has never heard of
  harbor-console.
- The allocator needs authoritative host state and gets it from a read-only
  `/ports.json`. When that is unreachable it refuses to grant rather than
  guessing — harbor-console can fail to allocate, but it cannot allocate wrongly.
- Ports already in use are grandfathered where they stand, so adoption renumbers
  nothing that is running.
