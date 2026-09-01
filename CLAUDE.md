# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Harbor Console is a lightweight operational console for a small fleet of Linux servers, with two surfaces over one core:

- **`harbor-console`** (shipped, v0.1.0) — a terminal dashboard that replaces the default Linux login console with an at-a-glance server health view (hostname, uptime, CPU/memory/disk, IPv4, Docker container count, clock). Refreshes once per second, exits cleanly on Ctrl+C.
- **`harbor-console ports`** (v0.2.0, shipped) — the port allocator. Projects declare what they need in `.harbor.toml`; harbor-console leases a port from `services.toml` and writes it into the project's own `.env` ([ADR 8](docs/adr/0008-allocate-ports-rather-than-validate.md)).
- **`harbor-console-web`** (v0.2.0, **specified but not yet implemented**) — a read-only status page served to the tailnet: the service directory from `services.toml`, live/down state, and drift against Docker.

`founding_document.txt` is the authoritative spec — read it before adding features. Under its "v0.2.0" heading the allocator is built; the web surface is still design, not code. `plan.md` is the handoff brief that motivated the expansion and records what is still open.

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
| Apply port assignments | `uv run harbor-console ports sync` |
| Print the lease table | `uv run harbor-console ports show` |

`pyproject.toml` sets `pythonpath = ["src"]`, so tests import `harbor_console` without an editable install.

## Architecture

Strict separation by responsibility — collect, render, coordinate — one job each. Keep it this way; it is what makes a second surface cheap.

Existing (implemented):

- `system.py` — **collects** metrics only. `collect_system_metrics()` returns a flat `dict[str, str | float | int]`. No rendering.
- `ui.py` — **renders** only. `build_dashboard(metrics)` turns the metrics dict into a `rich` renderable. No business logic, no metric collection.
- `app.py` — **coordinates** the refresh loop (`rich.live.Live`). No collection or rendering logic of its own.

The port allocator, implemented for v0.2.0, keeps the same split under `ports/`:

- `ports/keys.py`, `ports/ledger.py`, `ports/declaration.py` — **collect** the lease ledger (`services.toml`) and each project's declaration (`.harbor.toml`). A ledger that claims one `(host, addr, port)` twice is a hard error at load time.
- `ports/live.py`, `ports/discovery.py`, `ports/compose.py` — **collect** host state from `/ports.json`, the participating projects in the tree, and the ports each compose file publishes.
- `ports/allocate.py` — the allocation policy. Pure: no I/O, so every rule is testable with plain values.
- `ports/envfile.py`, `ports/explainer.py` — **render** the two generated artifacts: the managed fence in a project's `.env`, and `HARBOR_PORTS.md`.
- `ports/atomic.py` — the one way a whole file is replaced: temp file beside the target, then `os.replace`. Every writer goes through it ([ADR 9](docs/adr/0009-atomic-writes-and-env-last.md)).
- `ports/cli.py` — **coordinates** `scan` / `sync` / `show`, and is the **only module in the allocator that writes**. Nothing else touches the disk on its own.

Still planned for v0.2.0 (**none of these exist yet** — do not describe them as if they do):

- `docker.py` — **collects**: live container state for reconciliation against the ledger.
- `web.py` — **renders**: the HTML page, and serves it over stdlib `http.server`. No collection.
- `webapp.py` — **coordinates**: the background prober thread and the HTTP server, as the `harbor-console-web` systemd entry point.

The two processes share the core and have independent lifetimes — logging in at tty1 must not take the web page down, and vice versa. The web view is a second renderer over the same collectors, not a second application.

The dict returned by `collect_system_metrics()` is the contract between `system` and `ui`; its keys are asserted directly in `tests/test_system.py`. Changing a key means updating the collector, the renderer, and that test together.

### Dependency injection for testability

`app.run()` takes `collector`, `renderer`, and `sleep` as injectable parameters (defaulting to the real implementations). Tests drive the loop by passing fakes and raising `KeyboardInterrupt` from the fake `sleep` to exit after one iteration — no real time passes, no real metrics collected. Preserve this pattern when modifying the loop, and extend it to the web service: the prober and HTTP server get the same treatment, with no real sockets, no real time, and no real Docker in tests.

### Graceful degradation

Collectors never raise on a hostile environment: `get_docker_container_count()` returns `0` when the `docker` binary is missing or errors; `get_ipv4_address()` falls back to `127.0.0.1`. New collectors should follow suit — the dashboard "never crashes during normal operation" is a release criterion.

Two deliberate exceptions, where failing loudly is the point:

- A duplicate `(host, addr, port)` in `services.toml` is a hard error at load time, not a warning. Catching that collision is why the ledger exists.
- `harbor-console-web` refuses to start if it cannot bind the host's Tailscale address. There is no fallback to `0.0.0.0` — see the hard constraints below.

### The `.harbor-tmp.*` sweep pattern

`ports/atomic.py` writes to `.harbor-tmp.<target name>.<random>.tmp` beside the file it is replacing and removes it afterwards. A `SIGKILL` or a power cut leaves one behind, next to — and possibly containing part of — a `.env`. This repository ignores `.harbor-tmp.*`; **every participating project should add the same line to its own `.gitignore`**, because a rule for `.env` does not match a temp file derived from it. That pattern is also how you find and remove abandoned temp files; nothing sweeps them automatically.

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

How the port authority is enforced was answered on 2026-09-01: by allocation, which is the option that touches other repositories ([ADR 8](docs/adr/0008-allocate-ports-rather-than-validate.md)). One question is still open and deliberately undecided — do not answer it by writing code:

- **Where the ledger lives on disk** (in-repo and installed to `/opt/harbor-console/`, or a path under `/etc`). It is in-repo today because that is where `ports sync` runs, not because the question is settled.

Development follows TDD and "main is always deployable."

When you make or reverse a significant architectural decision, record it as an ADR in `docs/adr/` (Nygard format; copy `docs/adr/template.md`). ADRs are immutable once accepted — supersede rather than edit. Existing records explain the `rich` choice, the 1 Hz refresh, and the no-plugins/no-config stance.
