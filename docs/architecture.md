# Architecture

Harbor Console is two surfaces over one core, and the core is a strict split by
responsibility: **collect**, **render**, **coordinate**. Nothing does two of
those jobs.

## Shipped (v0.1.0)

- `system.py` collects all runtime metrics.
- `ui.py` only renders the dashboard from provided metrics.
- `app.py` runs a 1-second refresh loop and exits cleanly on `Ctrl+C`.

`harbor-console` runs this loop on tty1 under systemd.

## Shipped (v0.2.0): the port allocator

`harbor-console ports` hands out the published host ports for every
participating project in the tree. A project declares what it wants in its own
`.harbor.toml`; the allocator records a lease in `services.toml` and writes the
granted number into that project's `.env` behind a managed fence, which compose
reads as `"${HARBOR_PORT_NAME:-default}:container"`
([ADR 8](adr/0008-allocate-ports-rather-than-validate.md)).

It lives in `harbor_console/ports/` and repeats the same split:

- `keys.py`, `ledger.py`, `declaration.py` collect: the lease ledger
  (`services.toml`) and each project's declaration (`.harbor.toml`).
- `live.py`, `discovery.py`, `compose.py` collect host state: what is listening
  on the host (from a read-only `/ports.json`), which projects in the tree
  participate, and what each compose file publishes.
- `allocate.py` decides. It is pure — no I/O — so every allocation rule is
  testable with plain values.
- `envfile.py`, `explainer.py` render the two generated artifacts: the managed
  fence in a project's `.env`, and `HARBOR_PORTS.md`.
- `atomic.py` is the one way a whole file is replaced: a temp file beside the
  target, then `os.replace`. Every writer goes through it
  ([ADR 9](adr/0009-atomic-writes-and-env-last.md)).
- `cli.py` coordinates `scan` / `sync` / `show`, and is the **only** module in
  the allocator that writes. Nothing else touches the disk on its own.

There is no `registry.py`. Validating `services.toml` is `ports/ledger.py`, and
it is one half of a pair with `ports/declaration.py`.

Three properties of the allocator are structural:

- The uniqueness key is **`(host, addr, port)`**, not `(host, port)`. `0.0.0.0`
  is the wildcard and contends with every address on its host; two different
  specific addresses on one host do not contend; two hosts never contend. A
  ledger that contradicts itself under that rule is a hard error at load time,
  not a warning ([ADR 10](adr/0010-address-scoped-port-key.md)).
- `sync` writes a project whenever its files disagree with the ledger, not only
  when a decision changed — so a fresh clone with no `.env` is repaired rather
  than left to fall back to a compose default that may collide. A repair is
  reported as a repair, distinctly from a grant
  ([ADR 11](adr/0011-sync-repairs-drift-and-show-stands-alone.md)).
- `show` reads the ledger alone and loads no declarations, so a broken
  `.harbor.toml` in one project does not stop an operator reading the lease
  table — which is exactly when they need it (ADR 11).

## Shipped (v0.2.0): the tailnet status page

A second process, `harbor-console-web`, serves a read-only status page to the
tailnet, and serves the `/ports.json` the allocator reads. It reuses the same
collectors rather than duplicating them — which is the payoff of the split
above: a web view is a second renderer, not a second application.

Collect:

- `tailnet.py` — the host's Tailscale address, from `tailscale ip -4`. It is
  the one collector in the project permitted to **raise**: every other one
  degrades quietly, but a silent fallback here would publish an inventory of
  every service on the host to the whole LAN
  ([ADR 7](adr/0007-bind-tailscale-address-only.md)).
- `listening.py` — every listening TCP socket, from `psutil`. Only the host
  itself can see loopback-bound and non-Docker listeners. IPv6 `::` is
  normalised to `0.0.0.0`, the wildcard the ledger's overlap rule knows.
- `docker.py` — running containers and the host ports they publish. "Docker
  could not be asked" is a distinct result from "asked, nothing running": the
  difference decides whether the page may call a service undeclared.
- `probe.py` — liveness and optional detail for one service, by convention:
  `/` for up, `/hcstatus` for detail. Any HTTP response means up
  ([ADR 12](adr/0012-web-surface-collectors-and-conventions.md)).

Decide:

- `reconcile.py` — the drift policy, and pure, for the same reason
  `ports/allocate.py` is: leases, listeners and containers in, findings out.
  The join key is `(addr, port)`, compared by address overlap, because that is
  the key the ledger owns.

The contract:

- `snapshot.py` — data only, the handoff between the prober and the renderer.
  Its own module so neither side has to import the other, the way
  `ports/keys.py` serves the allocator. `Snapshot.probed` separates "collected,
  found nothing" from "collected nothing yet", and `Snapshot.collection_error`
  carries why a cycle failed, whatever its source — a collector, the prober or
  the ledger. It is not named for the ledger, because naming it that pointed
  every failure at `services.toml`.

Render and coordinate:

- `web.py` renders: one self-contained HTML page from a snapshot, plus the
  `/ports.json` body. It never collects and never probes.
- `webapp.py` coordinates: the background prober thread and the HTTP server, as
  the `harbor-console-web` systemd entry point.

Four properties of that service are structural rather than incidental:

- It binds the host's Tailscale address only, and **refuses to start** without
  it. There is no fallback, no `--host`, and no dev mode (ADR 7).
- That is one of **four** startup refusals, and there are only four: no tailnet
  address, a ledger that will not load, its own service not declared in that
  ledger, and its own service declared more than once. Each happens before
  anything is bound and exits non-zero with the reason on stderr, so systemd
  retries and the operator reads it in journald. The last two are about
  identity: binding a port no lease reserves is the collision the ledger exists
  to prevent, and running one page for several hosts needs an explicit choice
  of identity, which is deferred rather than guessed. (A leased port already in
  use fails the bind and refuses the same way; that is the environment, not a
  fifth rule.)
- The host it serves is decided **once**, from the lease this process holds,
  and passed down explicitly. It is never re-derived from
  `socket.gethostname()`: the ledger's `host` is a hand-authored string, and a
  name disagreeing with the OS — `hpz440` against `hpz440.lan` — would silently
  empty this host's share of the ledger.
- Probing never runs inside a request handler, so one hung service cannot make
  the page slow to load. Until the first probe cycle completes, `/ports.json`
  answers **503**: an unprobed snapshot has no listeners because none were
  looked for, and serving that as 200 would read to the allocator as a verified
  empty host and let it grant a port already in use.

The two processes have independent lifetimes. Logging in at the attached
monitor must not take the tailnet page down, and the page must not depend on
anyone being logged in.

See `founding_document.txt` for the full v0.2.0 specification, including the
questions it leaves deliberately open.

## Why

The reasoning behind these and other choices is recorded as Architecture
Decision Records in [`adr/`](adr/README.md).
