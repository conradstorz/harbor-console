# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Harbor Console is a lightweight operational console for a small fleet of Linux servers, with two surfaces over one core:

- **`harbor-console`** (shipped, v0.1.0) — a terminal dashboard that replaces the default Linux login console with an at-a-glance server health view (hostname, uptime, CPU/memory/disk, IPv4, Docker container count, clock). Refreshes once per second, exits cleanly on Ctrl+C.
- **`harbor-console ports`** (v0.2.0, shipped) — the port allocator. Projects declare what they need in `.harbor.toml`; harbor-console leases a port from `services.toml` and writes it into the project's own `.env` ([ADR 8](docs/adr/0008-allocate-ports-rather-than-validate.md)).
- **`harbor-console-web`** (v0.2.0, shipped) — a read-only status page served to the tailnet: the service directory from `services.toml`, live/down state, and drift against Docker. It also serves the `/ports.json` the allocator reads. Runs as its own systemd unit (`deploy/harbor-console-web.service`), bound to the host's Tailscale address only ([ADR 7](docs/adr/0007-bind-tailscale-address-only.md)), collecting by convention rather than from declarations the server does not have ([ADR 12](docs/adr/0012-web-surface-collectors-and-conventions.md)).

`founding_document.txt` is the authoritative spec — read it before adding features. Everything under its "v0.2.0" heading is now built. `plan.md` is the dated handoff brief that motivated the expansion; it is a historical record of how v0.2.0 was decided, not a description of the code — read it for reasoning, not for what exists (it still names a `registry.py` that was never written). The one question genuinely still open is under Scope discipline below.

## Commands

Uses `uv` (not pip/venv). Python 3.13+.

| Task | Command |
|------|---------|
| Install deps (incl. dev) | `uv sync --extra dev` |
| Run the dashboard | `uv run harbor-console` (or `uv run python -m harbor_console`) |
| Run all tests | `uv run pytest` |
| Run one test file | `uv run pytest tests/test_system.py` |
| Run one test | `uv run pytest tests/test_system.py::test_format_uptime` |
| Report pending port changes | `uv run harbor-console ports scan` |
| Apply port assignments, and repair drifted projects | `uv run harbor-console ports sync` |
| Print the lease table (reads no declarations) | `uv run harbor-console ports show` |
| Run the tailnet status page | `uv run harbor-console-web` (binds the Tailscale address; refuses without one) |

`pyproject.toml` sets `pythonpath = ["src"]`, so tests import `harbor_console` without an editable install.

## Architecture

Strict separation by responsibility — collect, render, coordinate — one job each. Keep it this way; it is what makes a second surface cheap.

Existing (implemented):

- `system.py` — **collects** metrics only. `collect_system_metrics()` returns a flat `dict[str, str | float | int]`. No rendering.
- `ui.py` — **renders** only. `build_dashboard(metrics)` turns the metrics dict into a `rich` renderable. No business logic, no metric collection.
- `app.py` — **coordinates** the refresh loop (`rich.live.Live`). No collection or rendering logic of its own.

The port allocator, implemented for v0.2.0, keeps the same split under `ports/`:

- `ports/keys.py`, `ports/ledger.py`, `ports/declaration.py` — **collect** the lease ledger (`services.toml`) and each project's declaration (`.harbor.toml`). The uniqueness key is `(host, addr, port)`, compared by address *overlap*: `0.0.0.0` contends with every address on its host, two different specific addresses do not contend, two hosts never contend ([ADR 10](docs/adr/0010-address-scoped-port-key.md)). A ledger that claims one `(host, addr, port)` twice is a hard error at load time.
- `ports/live.py`, `ports/discovery.py`, `ports/compose.py` — **collect** host state from `/ports.json`, the participating projects in the tree, and the ports each compose file publishes.
- `ports/allocate.py` — the allocation policy. Pure: no I/O, so every rule is testable with plain values.
- `ports/envfile.py`, `ports/explainer.py` — **render** the two generated artifacts: the managed fence in a project's `.env`, and `HARBOR_PORTS.md`.
- `ports/atomic.py` — the one way a whole file is replaced: temp file beside the target, then `os.replace`. Every writer goes through it ([ADR 9](docs/adr/0009-atomic-writes-and-env-last.md)).
- `ports/cli.py` — **coordinates** `scan` / `sync` / `show`, and is the **only module in the allocator that writes**. Nothing else touches the disk on its own.

Two behaviours of that CLI are load-bearing and easy to undo by accident ([ADR 11](docs/adr/0011-sync-repairs-drift-and-show-stands-alone.md)):

- **`sync` writes a project whose files have drifted, not only one whose decision changed.** `.env` is gitignored, so every fresh clone of a participating project starts without one while its lease stands and its decision is "keep"; writing only changes would report "up to date" over a project about to fall back to its compose default, on a port that may be leased to somebody else. A missing or mangled fence and a missing `HARBOR_PORTS.md` are repaired the same way. `scan` reports the same condition and writes nothing. A repair is reported *as* a repair, distinctly from a grant, and a tree that already matches stays a genuine no-op.
- **`show` loads no declarations at all.** It reads the ledger and prints it, so a broken `.harbor.toml` anywhere in the tree — which does fail `scan` and `sync` — still leaves an operator able to read the lease table, which is exactly when they need it.

The web surface, implemented for v0.2.0, is eight modules at the top level, and keeps the same split:

- `tailnet.py` — **collects** the host's Tailscale address from `tailscale ip -4`. The one collector allowed to raise (see Graceful degradation below).
- `listening.py` — **collects** every listening TCP socket via `psutil`, including loopback-bound and non-Docker ones. IPv6 `::` is normalised to `0.0.0.0`.
- `docker.py` — **collects** running containers and the host ports they publish. `DOCKER_UNAVAILABLE` distinguishes "could not ask Docker" from "asked, nothing running"; the difference decides whether the page may call a service undeclared.
- `probe.py` — **collects** liveness and optional detail for one service: `/` for up, `/hcstatus` for detail, both by convention ([ADR 12](docs/adr/0012-web-surface-collectors-and-conventions.md)). Any HTTP response means up.
- `reconcile.py` — the drift policy. Pure, like `ports/allocate.py`: leases, listeners and containers in, findings out. Joins on `(addr, port)` by address overlap.
- `snapshot.py` — the **contract** between prober and renderer, data only. Its own module so neither imports the other, the way `ports/keys.py` serves the allocator. `Snapshot.probed` separates "found nothing" from "not looked yet"; `Snapshot.collection_error` — not `ledger_error` — carries why a cycle failed, whatever its source.
- `web.py` — **renders** the HTML page from a snapshot and the `/ports.json` body, and serves both over stdlib `http.server`. No collection, no probing.
- `webapp.py` — **coordinates**: the background prober thread and the HTTP server, as the `harbor-console-web` systemd entry point.

Three behaviours of that service are load-bearing and easy to undo by accident:

- **The served host is decided once, in `webapp.main`, from the lease this process holds** — never from `socket.gethostname()`. The ledger's `host` is a hand-authored string; a name that disagrees with the OS (`hpz440` against `hpz440.lan`) would silently empty this host's share of the ledger and report every healthy container as undeclared.
- **`harbor-console-web` has four startup refusals, and only four:** no tailnet address, an unreadable ledger, its own service not declared, and its own service declared more than once. Every one of them happens before anything is bound, exits non-zero, and leaves the reason in journald for systemd to retry against. The last two are about identity: a page bound to a port no lease reserves is the collision the ledger exists to prevent, and multi-host operation needs an explicit choice of identity, which is deferred rather than guessed.
- **`/ports.json` answers 503 until the first probe cycle completes, and again whenever Docker could not be read.** An unprobed snapshot has no listeners because none were looked for; serving it as 200 would read to the allocator as a verified empty host and let it grant a port already in use. The second window is a snapshot that was collected but cannot attribute anything to a container, which is just as dangerous a 200. Both become the refusal `ports/live.py` already has.

The two processes share the core and have independent lifetimes — logging in at tty1 must not take the web page down, and vice versa. The web view is a second renderer over the same collectors, not a second application.

The dict returned by `collect_system_metrics()` is the contract between `system` and `ui`; its keys are asserted directly in `tests/test_system.py`. Changing a key means updating the collector, the renderer, and that test together.

### Dependency injection for testability

`app.run()` takes `collector`, `renderer`, and `sleep` as injectable parameters (defaulting to the real implementations). Tests drive the loop by passing fakes and raising `KeyboardInterrupt` from the fake `sleep` to exit after one iteration — no real time passes, no real metrics collected. Preserve this pattern when modifying the loop, and extend it to the web service: the prober and HTTP server get the same treatment, with no real sockets, no real time, and no real Docker in tests.

### Graceful degradation

Collectors never raise on a hostile environment: `get_docker_container_count()` returns `0` when the `docker` binary is missing or errors; `get_ipv4_address()` falls back to `127.0.0.1`. New collectors should follow suit — the dashboard "never crashes during normal operation" is a release criterion.

Three deliberate exceptions, where failing loudly is the point:

- A duplicate `(host, addr, port)` in `services.toml` is a hard error at load time, not a warning. Catching that collision is why the ledger exists.
- A `.harbor.toml` that cannot be parsed fails `scan` and `sync`: the allocator will not allocate against data it cannot read. `show` is deliberately exempt.
- `harbor-console-web` refuses to start on any of four conditions: no tailnet address, a ledger that will not load, its own service not declared in that ledger, and its own service declared more than once. There is no fallback to `0.0.0.0` — see the hard constraints below. `tailnet.py` is therefore the one collector that raises rather than degrading. A leased port that is already in use fails the bind and refuses the same way, but it is a failure of the environment rather than a fifth rule.

### The `.harbor-tmp.*` sweep pattern

`ports/atomic.py` writes to `.harbor-tmp.<target name>.<random>.tmp` beside the file it is replacing and removes it afterwards. A `SIGKILL` or a power cut leaves one behind, next to — and possibly containing part of — a `.env`. This repository ignores `.harbor-tmp.*`; **every participating project should add the same line to its own `.gitignore`**, because a rule for `.env` does not match a temp file derived from it. Since template version 2, `HARBOR_PORTS.md` tells each participating project that in its own words, which is what ADR 9 meant by "documented for them". That pattern is also how you find and remove abandoned temp files; nothing sweeps them automatically.

## Hard constraints (v0.2.0)

These are load-bearing decisions, not preferences. Changing one needs a new ADR.

- **The web service binds the Tailscale address only, never `0.0.0.0`, with no override and no dev-mode relaxation.** The page is an inventory of every service on the host; a silent broader bind publishes it to the whole LAN. Binding *is* the access control, which is why there is no login page ([ADR 7](docs/adr/0007-bind-tailscale-address-only.md)).
- **Stdlib `http.server` and `tomllib` only.** v0.2.0 adds no runtime dependency; FastAPI and uvicorn were rejected as disproportionate to one page. The ledger writer is hand-rolled for the same reason — `tomllib` reads TOML but cannot write it.
- **Every whole-file write is atomic, and a project's `.env` is written last.** `.env` is what makes a container bind the port; writing it before the ledger's record is safe would leave a project publishing a port the ledger no longer reserves ([ADR 9](docs/adr/0009-atomic-writes-and-env-last.md)).
- **Probing happens in a background thread, never inside a request handler.** One hung service must not make the status page slow to load.
- **The page is read-only.** The registry's authority is port allocation only — not container lifecycle, not access control. No buttons that do anything.
- **Health probing is dumb on purpose: any HTTP response means up.** GTE answers `/` with a 303 to `/login`; a probe insisting on 200 would call a healthy service down.

## Scope discipline

The project is deliberately minimal (MVP / YAGNI / KISS). `founding_document.txt` lists an explicit "Deferred Until Later" set — colors, themes, plugins, user configuration, interactive menus, service/Docker management, notifications, multi-host aggregation, and more. Do not add these without a demonstrated operational need. There are intentionally no colors, no keyboard shortcuts, no persistence, and no user-facing configuration.

v0.2.0 expanded the scope once, and the bar it cleared is the bar: a real collision on port 8080 that would have failed silently. `services.toml` is a declared authority owned by this repo, not user configuration of the dashboard — the no-config stance still holds for everything it originally targeted ([ADR 6](docs/adr/0006-service-registry-and-web-status-page.md) amends [ADR 3](docs/adr/0003-no-plugins-in-mvp.md)).

How the port authority is enforced was answered on 2026-09-01: by allocation, which is the option that touches other repositories ([ADR 8](docs/adr/0008-allocate-ports-rather-than-validate.md)). Where the ledger lives on disk was answered on 2026-09-02: it stays in-repo, deployed to `/opt/harbor-console/` by `deploy/install.sh` — nothing on the server writes it, so the deploy step is the only way a fresh copy reaches the server, and `/etc` would need the identical rsync to get there ([ADR 13](docs/adr/0013-ledger-lives-in-repo.md)). Both questions this section once carried are now decided; there is no open question here to leave unanswered.

Development follows TDD and "main is always deployable."

When you make or reverse a significant architectural decision, record it as an ADR in `docs/adr/` (Nygard format; copy `docs/adr/template.md`). ADRs are immutable once accepted — supersede rather than edit. Existing records explain the `rich` choice, the 1 Hz refresh, and the no-plugins/no-config stance.
