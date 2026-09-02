# Web surface — design

Date: 2026-09-02
Status: implemented. Shipped on `feat/web-surface`; see PR #5.

Where this document and the code disagree, **the code is right** — read it
for the reasoning, not as a description of what exists. Three things changed
during implementation: it names six modules and eight shipped (`reconcile.py`
holds the pure drift policy and `snapshot.py` is the prober-renderer contract
that keeps the two from importing each other); the served host is decided once
from this service's own lease rather than from the host's metrics; and
`/ports.json` answers 503 not only before the first probe cycle but also
whenever Docker could not be read, because an empty listener list and an
unasked question are indistinguishable to the allocator.

Builds `harbor-console-web`, the second surface of v0.2.0: a read-only status
page served to the tailnet, and the `/ports.json` endpoint the allocator already
consumes. The allocator half shipped in PR #4.

---

## Problem

Two gaps remain from [ADR 6](../../adr/0006-service-registry-and-web-status-page.md):

1. **No directory.** Nothing says what is running on hpz440, on which port, and
   whether it is up, without logging in.
2. **The allocator cannot verify host state.** `ports/live.py` fetches
   `/ports.json` from a service that does not exist, so every `sync` today runs
   degraded and refuses to grant. The page is what unblocks the allocator.

## Decisions

| Question | Decision |
| --- | --- |
| How is the Tailscale address found? | `tailscale ip -4`. Authoritative, and its absence is the refuse-to-start ADR 7 requires. |
| How are listening sockets enumerated? | `psutil.net_connections(kind="tcp")` filtered to LISTEN. Already a dependency; no subprocess, no parsing. |
| How does the page know what to probe? | By convention: `/` for liveness, `/hcstatus` for detail. Nothing new is deployed. |
| How does it reconcile? | Join leases against Docker's published ports on `(addr, port)` — the key the ledger owns. |
| Where does the service get its own port? | From the ledger, which declares `harbor-console/web`. It takes its port from the file it serves. |

---

## Architecture — six modules, flat

Same collect / render / coordinate split the rest of the project uses.

| Module | Job | On failure |
| --- | --- | --- |
| `listening.py` | **collect** listening sockets | empty list |
| `docker.py` | **collect** container names and published ports | empty list |
| `tailnet.py` | **collect** the host's Tailscale address | **raises — the only hard failure** |
| `probe.py` | **collect** liveness and `/hcstatus` for one service | down / no detail |
| `web.py` | **render** the page; serve it and `/ports.json` | — |
| `webapp.py` | **coordinate** the prober thread and HTTP server | — |

Flat rather than a `web/` package, so `docker.py` remains a shared collector the
terminal surface could also use. ADR 6 named three modules; the three extra are
all collectors, which is the existing discipline applied rather than widened.

`docker.py` is the only one of these the terminal dashboard could later reuse;
`system.py` currently counts containers with its own `docker ps -q`. **Leave that
alone.** Rewiring the shipped tty1 path is not this change's job.

### Data flow

```
webapp.main()
  ├─ tailnet.tailscale_address()   → fatal if unavailable
  ├─ ledger.load_leases()          → fatal if unreadable
  ├─ own port ← lease harbor-console/web
  ├─ start prober thread ──────────┐
  └─ serve on (tailnet_addr, port) │
                                   │
   every 30s: listening + docker + probe each lease
              → publish a frozen Snapshot
                                   │
   handler reads the last Snapshot ┘   — never probes
```

The handler never collects. One hung service must not make the page slow, which
is the whole reason the prober is a thread ([ADR 6](../../adr/0006-service-registry-and-web-status-page.md)).

`Snapshot` is the contract between the prober and the renderer, exactly as the
dict from `collect_system_metrics()` is the contract between `system` and `ui`.

---

## Data model

```python
@dataclass(frozen=True)
class Listener:          # listening.py
    addr: str
    port: int
    pid: int | None      # None for another user's socket; we run unprivileged

@dataclass(frozen=True)
class Container:         # docker.py
    name: str
    published: tuple[tuple[str, int], ...]   # (addr, host_port)

@dataclass(frozen=True)
class Detail:            # probe.py — one row from /hcstatus
    label: str
    value: str

@dataclass(frozen=True)
class Health:            # probe.py — one service's result
    up: bool
    state: str | None        # "ok" | "warn" | "error", from /hcstatus
    summary: str | None
    detail: tuple[Detail, ...]
    warning: str | None      # why /hcstatus was ignored, if it was

@dataclass(frozen=True)
class Drift:
    kind: str            # "declared-not-running" | "running-not-declared"
                         # | "port-mismatch"
    detail: str

@dataclass(frozen=True)
class Snapshot:
    collected: datetime
    metrics: dict[str, str | float | int]      # from collect_system_metrics()
    leases: tuple[Lease, ...]
    listeners: tuple[Listener, ...]
    containers: tuple[Container, ...]
    health: dict[tuple[str, str], Health]      # keyed (project, name)
    drift: tuple[Drift, ...]
    collection_error: str | None                # set when a cycle failed,
                                                  # whatever the source --
                                                  # ledger, collector, prober
                                                  # or reconciler
    probed: bool                                 # False until the first
                                                  # cycle completes
```

## `/ports.json`

**The allocator already consumes this, so the contract is fixed by
`ports/live.py`, not chosen here.** `fetch_live` reads `payload["host"]` and
`payload["listening"]`, and for each entry `addr`, `port` and an optional
`container`. It rejects a `port` that is not a real integer, so the emitter must
never produce a float, a bool, or a numeric string.

```json
{
  "host": "hpz440",
  "collected": "2026-09-02T14:02:11Z",
  "listening": [
    {"addr": "0.0.0.0",         "port": 8080,  "container": "gte"},
    {"addr": "127.0.0.1",       "port": 5432,  "container": "shared-postgres"},
    {"addr": "100.69.239.123",  "port": 49152, "container": "arm-rippers-dev"},
    {"addr": "0.0.0.0",         "port": 22,    "container": null}
  ]
}
```

`container` is `null` for a non-Docker listener. sshd and tailscaled hold ports
too, and an allocator blind to them would eventually hand one out — that is why
the socket list comes from psutil rather than from Docker.

Attribution is by `(addr, port)` against Docker's published ports, not by PID:
running unprivileged, `pid` is `None` for other users' sockets, and container
processes are not ours.

Served from the last snapshot like every other route, so it is never slower than
the page.

## Probing

For each lease, `GET http://<host>:<port>/` with a **2 second timeout**. **Any
HTTP response means up** — including a 404 or a 303 to a login page. A probe
insisting on 200 would call GTE down.

Then `GET .../hcstatus`, also 2s. It only ever *adds* detail:

```json
{"state": "ok",
 "summary": "3 queued",
 "detail": [{"label": "queue", "value": "3"}]}
```

`state` is `ok` / `warn` / `error`; `summary` is one line; `detail` is label/value
pairs rendered verbatim. **A missing, slow, malformed, or wrong-shaped
`/hcstatus` never makes a service DOWN** — it shows up with a `warning`. A 404 is
the ordinary case, not an error, and must produce no warning at all.

Probes run in the prober thread, never in a handler. Every probe failure is
caught: a hung service costs one 2s timeout and nothing else.

## Reconciliation

Three categories, joined on `(addr, port)` using `keys.addrs_overlap` so a
`0.0.0.0` publish matches a specific-address lease:

1. **declared-not-running** — a lease with nothing listening on it.
2. **running-not-declared** — a container publishing a port no lease covers.
3. **port-mismatch** — a container publishing a port other than the one its
   project's lease reserves. This is the original failure mode.

Category 3 needs to know which container belongs to which project, and the
ledger does not carry container names — that is the descriptive metadata this
design deliberately does not deploy. Resolve it without adding any: report a
port-mismatch only when a container's name **equals** a lease's project name.
`gte` matches `gte`; `arm-rippers-dev` does not match
`automatic-ripping-machine`.

An unmatched pair is not lost, it is simply reported as what it literally is —
category 1 and category 2, the same event seen from both sides. That is honest
and needs no field that could go stale. If name-matching later proves too
coarse, the fix is a `container` field on the lease, and it needs an ADR.

The allocator's fourth category — the project's `.env` disagreeing with its
lease — is **out of scope here**: it needs the `.harbor.toml` and `.env` files,
which exist only on the dev box. `ports scan` reports it; the page cannot.

Docker unavailable degrades honestly: categories 2 and 3 are suppressed and the
page says Docker could not be read, rather than claiming nothing is running.

## The page

One self-contained HTML document. Inline CSS, no JavaScript, no external assets,
`<meta http-equiv="refresh" content="30">`.

1. **Host** — the same metrics as the terminal dashboard, from
   `collect_system_metrics()`.
2. **Services** — one row per lease: project/name, `addr:port` linked to
   `http://<host>:<port>/`, UP or DOWN, and any `/hcstatus` summary with its
   detail rows beneath.
3. **Drift** — the three categories, or an explicit "no drift" line.
4. **Collected at** — the snapshot timestamp, so a stale page is visibly stale.

Links use the ledger's `host`, which MagicDNS resolves from anywhere on the
tailnet. That is why no discovery protocol was needed.

The page is **read only**. No forms, no buttons, no state-changing routes. Port
allocation authority was chosen over lifecycle control
([ADR 6](../../adr/0006-service-registry-and-web-status-page.md)).

Unknown paths return 404. Only `/` and `/ports.json` exist.

## Binding

`tailnet.tailscale_address()` runs `tailscale ip -4` and returns the first line,
validated as an IPv4 address. Anything else — binary missing, daemon down,
non-zero exit, unparseable output — raises `TailnetUnavailable`.

`webapp.main()` lets that propagate and exits non-zero. **There is no fallback
bind, no `--host` override, and no development mode**, because the page is an
inventory of every service on the host and a silent broader bind publishes it to
the whole LAN. Binding *is* the access control, which is why there is no login
page ([ADR 7](../../adr/0007-bind-tailscale-address-only.md)).

The port comes from the ledger's `harbor-console/web` lease. If that lease is
absent, refuse to start: every service is declared, including this one.

## Failure behaviour

| Condition | Behaviour |
| --- | --- |
| Tailscale address unavailable | exit non-zero at startup; systemd retries |
| Ledger unreadable at startup | exit non-zero |
| `harbor-console/web` lease ambiguous (more than one host declares it) | exit non-zero, naming each lease's host, address and port |
| Ledger unreadable on reload, or any other collection failure | keep the last good directory, set `collection_error`, show a banner. Before the first cycle has ever completed, the banner still names the failure but drops "showing the last good page" -- there is no last good page yet |
| `harbor-console/web` lease missing | exit non-zero |
| Before the first collection cycle completes (`probed` is False) | the page renders the starting placeholder and says so; `/ports.json` returns **503** rather than an empty, falsely-authoritative listener list -- `urllib` raises `HTTPError`, `ports.live.fetch_live` turns that into `LiveUnavailable`, and the allocator falls back to its existing refusal instead of granting against a host nothing has looked at yet |
| Docker missing or erroring | empty container list; drift categories 2–3 suppressed with a note |
| Socket enumeration denied | empty listener list; the page says so |
| A service hangs | 2s timeout, DOWN, page unaffected |
| `/hcstatus` broken | service still UP, with a warning |

Collectors never raise on a hostile environment — the one deliberate exception
is the bind address, where failing loudly is the point.

## Deployment

`deploy/harbor-console-web.service`:

```ini
[Unit]
Description=Harbor Console web status page
After=tailscaled.service network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/opt/harbor-console/.venv/bin/harbor-console-web
Restart=always
RestartSec=2
User=harbor
Group=harbor
SupplementaryGroups=docker
NoNewPrivileges=yes
ProtectHome=yes
PrivateTmp=yes
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

`StartLimitIntervalSec=0` is **load-bearing, not boilerplate**. `Restart=always`
with `RestartSec=2` otherwise trips systemd's default start limit (5 starts in
10s) and gives up permanently. This service flaps by design while `tailscaled`
acquires an address, which is exactly that scenario. The tty1 unit carries the
same line for the same reason.

No `TTYPath`, no `Conflicts=getty@tty1` — this unit must not touch the console.

`deploy/install.sh` installs both units, stays idempotent, and **must not change
the tty1 masking**. `pyproject.toml` gains the `harbor-console-web` entry point.

The two processes have independent lifetimes: restarting one must not disturb
the other.

## Testing

Following the injected-fakes pattern of `app.run()` — no real sockets, no real
HTTP, no real time, no real Docker:

- `listening.py`, `docker.py`, `tailnet.py`: the psutil call, the `subprocess.run`
  and the `tailscale` invocation are injected or monkeypatched. Cover the denied,
  missing-binary, non-zero-exit and garbage-output paths.
- `probe.py`: injected opener. Cover any-response-is-up (200, 404, 303), timeout,
  connection refused, and `/hcstatus` that is absent, malformed, wrong-shaped and
  slow — the last four must all yield UP with a warning, and a 404 must yield no
  warning at all.
- Reconciliation is pure: leases + listeners + containers in, `Drift` out. Tested
  with plain values, including the `0.0.0.0`-overlap cases.
- `web.py`: the renderer takes a `Snapshot` and returns bytes. Tested by building
  a snapshot directly — no server, no socket. Assert `/ports.json` round-trips
  through `ports.live.fetch_live` unchanged, which pins the two halves together.
- `webapp.py`: the prober loop is driven by an injected `sleep` that raises
  `KeyboardInterrupt` after one iteration, exactly as `tests/test_app.py` drives
  the dashboard. The bind is asserted by injecting a fake server factory and
  checking the address it was handed — never by opening a port.

## Out of scope

Lifecycle control, access control, authentication, any write route, multi-host
aggregation, historical data, and rewiring `system.py`'s container count.
