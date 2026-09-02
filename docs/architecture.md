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

## Specified, not yet implemented (v0.2.0)

A second process, `harbor-console-web`, serves a read-only status page to the
tailnet. It reuses the same collectors rather than duplicating them — which is
the payoff of the split above: a web view is a second renderer, not a second
application.

- `docker.py` collects: live container state, for reconciliation against the
  declaration.
- `web.py` renders: one self-contained HTML page, served over stdlib
  `http.server`.
- `webapp.py` coordinates: a background prober thread and the HTTP server.

It also serves the `/ports.json` the allocator reads, which is why an allocation
run today can be refused for want of authoritative host state.

The two processes have independent lifetimes. Logging in at the attached
monitor must not take the tailnet page down, and the page must not depend on
anyone being logged in.

Two properties are structural rather than incidental: the page binds the host's
Tailscale address only and refuses to start otherwise, and probing never runs
inside a request handler.

See `founding_document.txt` for the full v0.2.0 specification, including the
questions it leaves deliberately open.

## Why

The reasoning behind these and other choices is recorded as Architecture
Decision Records in [`adr/`](adr/README.md).
