# Port authority — design

Date: 2026-09-01
Status: approved, not implemented

Answers Section 3 of `plan.md` ("enforcing the port authority", previously OPEN)
and revises Section 1. The registry stops being a hand-authored list and becomes
a **lease ledger** over declarations the projects make themselves.

---

## Problem

Two projects publish host port 8080 on hpz440 and nothing says so. The loser of
the race binds nothing, logs it, and keeps running — so the symptom is a
dashboard that never appears on a service that otherwise looks healthy. There is
no authority over port assignment and no directory of what runs where.

## Grounding facts (verified 2026-09-01 — do not re-derive)

- **The project tree exists only on the Windows dev box.** `GTE/scripts/deploy.ps1`
  targets `docker --context hpz440` over `ssh://gte@hpz440`, sending the local
  tree as build context; compose reads `./docker-compose.yml` and `./.env`
  client-side. `automatic-ripping-machine/docker-compose.hpz440-prod.yml` does
  the same ("Bind mounts and ports bind on the DAEMON host, not locally").
  hpz440 holds images, containers and volumes — not checkouts.
- **Ports are claimed on one machine and observed on another.** This splits the
  system in two, and is the single fact that shapes the whole design.
- `automatic-ripping-machine` publishes `100.69.239.123:49152:8080` — bound to
  the Tailscale address specifically. A `(host, port)` key cannot express this;
  the key must include the bind address.
- 49152 is **inside** the Linux ephemeral range (32768–60999) and can lose a
  race to an outbound socket. New allocations must avoid that range.
- `shared-postgres` publishes nothing at all — internal compose network only.
  Services with no published port cannot collide and have no health URL.
- Several projects have multiple compose variants (`prod`, `dev`, `test`), so
  "the project's port" is not a single unambiguous fact readable from disk.
- harbor-console has **no CI**, and only 7 of ~35 repos in the tree do. "Fail it
  in CI" is not an available venue.
- `harbor-console` lives inside the same tree it scans
  (`programming/harbor-console`), so the tree root needs no configuration.

## Decisions

| Question | Decision |
| --- | --- |
| How does a project get a port? | It declares the need in `.harbor.toml`; harbor-console **allocates and writes the number back**. |
| How does the number reach compose? | Through a `HARBOR_PORT_*` variable in the project's `.env`, interpolated with a default so the project runs without harbor-console. |
| Who wins a conflict? | The **incumbent lease holder**. The newcomer is moved. Nothing running is ever renumbered. |
| How does the allocator know what is in use? | `harbor-console-web` serves a read-only `/ports.json` from hpz440. |
| What may an unattended run write? | **New grants only.** Reassignments always wait for a command you typed. |
| What is `/hcstatus`? | An optional JSON detail endpoint. It never gates liveness. |
| How do other repos learn the rules? | A static `HARBOR_PORTS.md` at each participating project's root. |

---

## Architecture — two halves

|  | Allocator | Observer |
| --- | --- | --- |
| Where | Windows dev box, this repo | hpz440, under systemd |
| Entry point | `harbor-console ports …` | `harbor-console-web` |
| Reads | the tree, the ledger, `GET /ports.json` | the ledger, listening sockets, Docker |
| Writes | the ledger, `.harbor.toml`, `.env`, `HARBOR_PORTS.md` | **nothing** |

Leases flow **dev box → git → `deploy/install.sh` → hpz440**. Live state flows
back **hpz440 → `/ports.json` → dev box**, read-only. Nothing ever writes to the
server, so [ADR 7](../../adr/0007-bind-tailscale-address-only.md) — the page is
read-only and tailnet-bound — holds unchanged.

The allocator is useful with the observer down (it falls back to the ledger plus
a direct TCP probe of candidate ports, and says so). The observer is useful with
the allocator never run (it shows the ledger against reality). Neither blocks the
other, matching the existing independence rule between `harbor-console` and
`harbor-console-web`.

### Module placement

```
src/harbor_console/
  ports/
    declaration.py   read/write .harbor.toml          collect + write
    ledger.py        read/write services.toml leases  collect + write
    allocate.py      pure allocation decision         no I/O
    envfile.py       managed-fence read/write         write
    explainer.py     HARBOR_PORTS.md template + write write
    cli.py           `harbor-console ports` commands  coordinate
  listening.py       listening sockets on this host   collect (server)
  web.py             page + /ports.json               render (server)
```

`allocate.py` takes declarations + leases + live state and returns a list of
decisions. It performs no I/O, so the whole allocation policy is testable with
plain dicts. Everything that touches a file is a thin writer around it. This is
the existing collect / render / coordinate discipline applied to a third surface.

---

## Data model — three files, three owners

### 1. `<project>/.harbor.toml` — the request (committed)

```toml
project = "imageharbor"
host    = "hpz440"

[[port]]
name          = "dashboard"          # → HARBOR_PORT_DASHBOARD
want          = 8080                 # preference, not a claim
assigned      = 8090                 # written by harbor-console
addr          = "0.0.0.0"            # optional; default 0.0.0.0
container     = "imageharbor"
health_path   = "/"
hcstatus_path = "/hcstatus"          # optional
description   = "Photo organiser dashboard"
```

`want` is human-owned and never rewritten. `assigned` is harbor-owned and never
hand-edited. Keeping them apart is what stops a stale preference from being
mistaken for a granted lease and re-fought every cycle.

A project with no published ports (`shared-postgres`) declares
`[[port]]`-free — it appears in the directory with no port and is never
allocated to or probed.

Variable name derivation: `HARBOR_PORT_` + `name` uppercased with every
non-alphanumeric character replaced by `_`.

### 2. `<project>/.env` — the effective value (often gitignored)

```
# >>> harbor-console (managed) >>>
HARBOR_PORT_DASHBOARD=8090
# <<< harbor-console (managed) <<<
```

Everything outside the fence is preserved byte-for-byte; the writer only ever
rewrites lines between the markers. If `.env` does not exist it is created
containing only the fence. If the fence is absent it is appended at the end.

Compose consumes it with a default:

```yaml
ports:
  - "${HARBOR_PORT_DASHBOARD:-8080}:8080"
```

The default is what makes the project independent of harbor-console: a fresh
clone with no `.env` still starts. It is a fallback, not the deployment path —
deploys run from this box, where `.env` exists.

### 3. `harbor-console/services.toml` — the ledger (committed here)

```toml
[[lease]]
project = "gte"
name    = "console"
host    = "hpz440"
addr    = "0.0.0.0"
port    = 8080
granted = 2026-07-05
```

**The lease key is `(host, addr, port)`.** A lease on `0.0.0.0` conflicts with
every address on that host; a lease on a specific address conflicts only with
itself and with `0.0.0.0`. This is what lets ARM hold `100.69.239.123:49152`
without claiming 49152 from everything else.

`granted` is what adjudicates a conflict, and committing the ledger means "who
held 8080 first" is answerable from git history.

### 4. `<project>/HARBOR_PORTS.md` — the explainer (committed)

Byte-identical in every participating project, with no project-specific numbers,
so it can never go stale. It carries a version line; `ports sync` rewrites it
only when the template version is newer than the file's, and says so. Full text
in Appendix A.

---

## Allocation

**Band: 8100–8999** for every new grant. Deliberately below the Linux ephemeral
range so an allocated port can never lose a race to an outbound socket.

**A port is free when it is neither leased nor listening.** Both conditions, not
either: a leased-but-stopped service keeps its port, which is exactly the failure
mode a naive port scan would cause. Liveness never revokes a lease.

**Grandfathering.** The first `ports sync` records what already exists — 8080,
8000, 8501, 8502, 5743, 26123, 1883, and ARM's `100.69.239.123:49152` — as leases
where they stand. Nothing running is renumbered to tidy the band. ARM's
ephemeral-range port is reported as a standing warning, not moved.

**Resolution order** for one declared port:

1. `assigned` already set and its lease is held by this project → keep it.
2. `want` is free → grant `want`.
3. Otherwise → the lowest free port in 8100–8999.
4. Band exhausted → hard error, no partial write.

**Conflict.** The lease with the earlier `granted` date wins. The newcomer gets a
new port; the incumbent is untouched. Output names both sides and the incumbent's
grant date rather than silently renumbering.

**Writes are all-or-nothing per project.** Ledger, `.harbor.toml` and `.env` for
one project are written together or not at all, so a crash cannot leave a lease
recorded with no `.env` to match.

## Commands

| Command | Writes | Use |
| --- | --- | --- |
| `harbor-console ports scan` | nothing | report: grants pending, conflicts, drift |
| `harbor-console ports sync` | everything | apply grants and reassignments |
| `harbor-console ports sync --new-only` | new grants only | what the scheduled task runs |
| `harbor-console ports show` | nothing | the current directory and lease table |

Bare `harbor-console` still launches the tty1 dashboard — the systemd unit is
unaffected.

**Unattended behaviour.** A Windows Task Scheduler entry runs
`ports sync --new-only` hourly. A declaration with no `assigned` yet is granted
and written automatically: that is the handout working on request. Anything that
would *change* an existing assignment is reported and left for a command you
type, so a timer can never renumber a project you are mid-deploy on.

**Tree discovery.** The allocator scans the direct children of its own parent
directory — harbor-console lives in the tree it scans — for `.harbor.toml`.
`HARBOR_TREE_ROOT` overrides it. Participation is opt-in by the file's presence,
so worktrees, archives and third-party checkouts are excluded by doing nothing.

**Degraded mode.** If `/ports.json` is unreachable the allocator says so, falls
back to the ledger plus a TCP probe of candidate ports, and refuses to grant a
port it cannot verify as unheld. It never guesses silently.

---

## Reconciliation

The page and `ports scan` name four disagreements:

1. **Declared, not running** — a lease with nothing listening.
2. **Running, not declared** — a listener with no lease.
3. **Running on a port that does not match its lease** — the original failure.
4. **Leased, but the project's `.env` says otherwise** — a granted port that was
   never redeployed. New with this design, and the most likely everyday drift.

One further warning class, not counted as drift: **compose default ≠ assigned**.
`.env` may be gitignored, so a clone without it falls back to the compose
default. `ports scan` parses the published ports of each project's compose files
and warns when a default no longer matches the assignment.

## `/ports.json` (served by harbor-console-web)

```json
{
  "host": "hpz440",
  "collected": "2026-09-01T14:02:11Z",
  "listening": [
    {"addr": "0.0.0.0",          "port": 8080,  "container": "gte"},
    {"addr": "127.0.0.1",        "port": 5432,  "container": "shared-postgres"},
    {"addr": "100.69.239.123",   "port": 49152, "container": "arm-rippers-dev"},
    {"addr": "0.0.0.0",          "port": 22,    "container": null}
  ]
}
```

Read-only, tailnet-bound like the rest of the page, refreshed by the same
background pass. `container` is `null` for non-Docker listeners — sshd and
tailscaled hold ports too, and an allocator blind to them would hand one out.

## `/hcstatus` (offered by projects, optional)

```json
{"state": "ok",
 "summary": "3 queued",
 "detail": [{"label": "queue", "value": "3"},
            {"label": "last run", "value": "14:02"}]}
```

`state` is one of `ok`, `warn`, `error`. `summary` is one short line. `detail` is
an ordered list of label/value pairs the project chooses; harbor-console renders
them verbatim and interprets none of them.

Fetched by the server-side prober on its own interval, never inside a request.
**It never gates liveness**: liveness remains "any HTTP response to `health_path`
means up", so a project whose `/hcstatus` is broken, absent, or unparseable shows
UP with a warning — never DOWN. Malformed JSON, a wrong shape, a timeout, and a
404 are all the same non-event.

---

## Onboarding a project

1. Add `.harbor.toml` declaring each port, with `want` set to what it uses today.
2. Change compose to `"${HARBOR_PORT_NAME:-<current port>}:<container port>"`.
3. Run `harbor-console ports sync`. It writes `assigned`, the `.env` fence, the
   ledger entry, and drops `HARBOR_PORTS.md`.
4. Redeploy when convenient. Until then drift category 4 shows the gap.

Roughly nine projects participate. GTE keeps 8080 as the incumbent; ImageHarbor
is the first real reassignment.

## Testing

Following the existing injected-fakes pattern — no real sockets, no real time, no
real Docker, and no writes to real repositories:

- `allocate.py` is pure: policy tests are plain dicts in, decisions out. Conflict
  precedence, band exhaustion, grandfathering, and the leased-but-stopped rule
  are all covered here.
- File writers are tested against a temporary tree: fence preservation, absent
  `.env`, absent fence, unrelated content untouched, all-or-nothing rollback.
- `/ports.json` is a fixture, not a fetch. Degraded mode is tested by making the
  fetch raise.
- `/hcstatus` handling is tested with malformed JSON, wrong shape, timeout and
  404 — all four must yield UP-with-warning.
- The prober is driven by an injected clock and an injected fetcher, the way
  `app.run()` is driven by an injected `sleep`.

## Consequences to record

This is the option `plan.md` warned couples other repositories, chosen
deliberately. Each participating project gains `.harbor.toml`, `HARBOR_PORTS.md`,
a `.env` fence and a one-line compose change, and harbor-console writes into
repositories it does not own.

Needs an ADR (0008) for the allocation model, and an update to
`founding_document.txt`: Section 1's registry becomes a lease ledger derived from
project declarations, and the uniqueness key gains the bind address.

Both of the founding document's open questions are answered here. Enforcement is
allocation: the registry is binding because it is the thing that hands out the
number, not a file anyone must remember to consult. And the ledger lives in this
repository, committed, deployed to `/opt/harbor-console/services.toml` by
`install.sh` — the same idempotent path everything else takes.

## Out of scope

Lifecycle control, access control, multi-host allocation (the schema carries
`host`; one host is served), UDP, dynamic port ranges, and any write path from
the server back to the allocator.

---

## Appendix A — `HARBOR_PORTS.md` template

    # Harbor Console — port assignment

    harbor-console-template-version: 1

    This file is placed in every project that participates in port assignment.
    It is identical everywhere and contains no numbers. It is written by
    harbor-console; edits are overwritten when the template version changes.

    ## Why this exists

    Published host ports on hpz440 are assigned centrally, not chosen per
    project. Two projects once claimed port 8080. The loser bound nothing,
    logged it, and kept running — so the only symptom was a dashboard that
    never appeared on a service that otherwise looked healthy. Nothing decided
    who owned the port, and nothing checked before the second project claimed
    it.

    ## The three files

    | File | Holds | Owned by |
    | --- | --- | --- |
    | `.harbor.toml` (this project) | what this project **wants** | you |
    | `.env` (this project) | what it **got** — the effective number | harbor-console |
    | `services.toml` (harbor-console) | the **lease** — who holds what, since when | harbor-console |

    `.harbor.toml` also carries an `assigned` field. `want` is yours and is
    never rewritten; `assigned` is harbor-console's and must not be hand-edited.

    ## Rules

    - **Do not hard-code a published port in compose.** Use the variable with a
      default: `"${HARBOR_PORT_NAME:-1234}:1234"`. The default is what lets this
      project start on a machine where harbor-console has never run.
    - **Do not edit inside the `# >>> harbor-console (managed) >>>` fence** in
      `.env`. It is rewritten on every sync. Everything outside it is preserved.
    - **To change a port**, edit `want` in `.harbor.toml`, then run
      `harbor-console ports sync` from the harbor-console checkout. You may not
      get what you asked for — if another project already holds it, you are
      moved and told so.
    - **The incumbent always wins.** A port already leased is never taken from
      the holder, and a running service is never renumbered underneath you.
    - **A stopped service keeps its port.** Ports are not reclaimed because
      nothing is listening.
    - **New ports come from 8100–8999**, deliberately below the Linux ephemeral
      range (32768–60999) so an assigned port cannot lose a race to an outbound
      socket.
    - **After a reassignment, redeploy.** Until you do, the running container
      still holds the old port and harbor-console will report the mismatch.

    ## Health and status endpoints

    harbor-console probes each declared port to show whether this project is up.

    - `health_path` (usually `/`) — **any HTTP response means up**, including a
      redirect to a login page. A probe insisting on 200 would call a healthy
      service down.
    - `hcstatus_path` (optional, conventionally `/hcstatus`) — richer detail,
      rendered on the status page. It never affects up/down: if it is missing,
      broken, slow, or malformed, this project still shows as up, with a
      warning. Return:

          {"state": "ok",
           "summary": "3 queued",
           "detail": [{"label": "queue", "value": "3"},
                      {"label": "last run", "value": "14:02"}]}

      `state` is `ok`, `warn`, or `error`. `summary` is one short line. `detail`
      is a list of label/value pairs of your choosing, rendered verbatim.

    ## If harbor-console is gone

    Nothing here breaks. The compose default keeps the project running, the
    numbers in `.env` and `.harbor.toml` stay valid, and this file explains the
    convention well enough to keep following it by hand.
