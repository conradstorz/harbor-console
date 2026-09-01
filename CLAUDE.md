# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Harbor Console is a lightweight operational console for a small fleet of Linux servers, with two surfaces over one core:

- **`harbor-console`** (shipped, v0.1.0) — a terminal dashboard that replaces the default Linux login console with an at-a-glance server health view (hostname, uptime, CPU/memory/disk, IPv4, Docker container count, clock). Refreshes once per second, exits cleanly on Ctrl+C.
- **`harbor-console-web`** (v0.2.0, **specified but not yet implemented**) — a read-only status page served to the tailnet: the service directory from a declared `services.toml` registry, live/down state, and drift against Docker.

`founding_document.txt` is the authoritative spec — read it before adding features. Everything under its "v0.2.0" heading is design, not code; nothing in this repo implements it yet. `plan.md` is the handoff brief that motivated the expansion and records what is still open.

## Commands

Uses `uv` (not pip/venv). Python 3.13+.

| Task | Command |
|------|---------|
| Install deps (incl. dev) | `uv sync --extra dev` |
| Run the dashboard | `uv run harbor-console` (or `uv run python -m harbor_console`) |
| Run all tests | `uv run pytest` |
| Run one test file | `uv run pytest tests/test_system.py` |
| Run one test | `uv run pytest tests/test_system.py::test_format_uptime` |

`pyproject.toml` sets `pythonpath = ["src"]`, so tests import `harbor_console` without an editable install.

## Architecture

Strict separation by responsibility — collect, render, coordinate — one job each. Keep it this way; it is what makes a second surface cheap.

Existing (implemented):

- `system.py` — **collects** metrics only. `collect_system_metrics()` returns a flat `dict[str, str | float | int]`. No rendering.
- `ui.py` — **renders** only. `build_dashboard(metrics)` turns the metrics dict into a `rich` renderable. No business logic, no metric collection.
- `app.py` — **coordinates** the refresh loop (`rich.live.Live`). No collection or rendering logic of its own.

Planned for v0.2.0 (**none of these exist yet** — do not describe them as if they do):

- `registry.py` — **collects**: reads and validates `services.toml`. `(host, port)` uniqueness is a hard error at load time.
- `docker.py` — **collects**: live container state for reconciliation against the registry.
- `web.py` — **renders**: the HTML page, and serves it over stdlib `http.server`. No collection.
- `webapp.py` — **coordinates**: the background prober thread and the HTTP server, as the `harbor-console-web` systemd entry point.

The two processes share the core and have independent lifetimes — logging in at tty1 must not take the web page down, and vice versa. The web view is a second renderer over the same collectors, not a second application.

The dict returned by `collect_system_metrics()` is the contract between `system` and `ui`; its keys are asserted directly in `tests/test_system.py`. Changing a key means updating the collector, the renderer, and that test together.

### Dependency injection for testability

`app.run()` takes `collector`, `renderer`, and `sleep` as injectable parameters (defaulting to the real implementations). Tests drive the loop by passing fakes and raising `KeyboardInterrupt` from the fake `sleep` to exit after one iteration — no real time passes, no real metrics collected. Preserve this pattern when modifying the loop, and extend it to the web service: the prober and HTTP server get the same treatment, with no real sockets, no real time, and no real Docker in tests.

### Graceful degradation

Collectors never raise on a hostile environment: `get_docker_container_count()` returns `0` when the `docker` binary is missing or errors; `get_ipv4_address()` falls back to `127.0.0.1`. New collectors should follow suit — the dashboard "never crashes during normal operation" is a release criterion.

Two deliberate exceptions, where failing loudly is the point:

- A duplicate `(host, port)` in `services.toml` is a hard error at load time, not a warning. Catching that collision is why the registry exists.
- `harbor-console-web` refuses to start if it cannot bind the host's Tailscale address. There is no fallback to `0.0.0.0` — see the hard constraints below.

## Hard constraints (v0.2.0)

These are load-bearing decisions, not preferences. Changing one needs a new ADR.

- **The web service binds the Tailscale address only, never `0.0.0.0`, with no override and no dev-mode relaxation.** The page is an inventory of every service on the host; a silent broader bind publishes it to the whole LAN. Binding *is* the access control, which is why there is no login page ([ADR 7](docs/adr/0007-bind-tailscale-address-only.md)).
- **Stdlib `http.server` and `tomllib` only.** v0.2.0 adds no runtime dependency; FastAPI and uvicorn were rejected as disproportionate to one page.
- **Probing happens in a background thread, never inside a request handler.** One hung service must not make the status page slow to load.
- **The page is read-only.** The registry's authority is port allocation only — not container lifecycle, not access control. No buttons that do anything.
- **Health probing is dumb on purpose: any HTTP response means up.** GTE answers `/` with a 303 to `/login`; a probe insisting on 200 would call a healthy service down.

## Scope discipline

The project is deliberately minimal (MVP / YAGNI / KISS). `founding_document.txt` lists an explicit "Deferred Until Later" set — colors, themes, plugins, user configuration, interactive menus, service/Docker management, notifications, multi-host aggregation, and more. Do not add these without a demonstrated operational need. There are intentionally no colors, no keyboard shortcuts, no persistence, and no user-facing configuration.

v0.2.0 expanded the scope once, and the bar it cleared is the bar: a real collision on port 8080 that would have failed silently. `services.toml` is a declared authority owned by this repo, not user configuration of the dashboard — the no-config stance still holds for everything it originally targeted ([ADR 6](docs/adr/0006-service-registry-and-web-status-page.md) amends [ADR 3](docs/adr/0003-no-plugins-in-mvp.md)).

Two questions are open and deliberately undecided — do not answer them by writing code:

- **How the port authority is enforced** (advisory check command, the drift list alone, or generated compose config). Only one of those options touches other repositories, so this decides the project's blast radius.
- **Where the registry lives on disk** (in-repo and installed to `/opt/harbor-console/`, or a path under `/etc`).

Development follows TDD and "main is always deployable."

When you make or reverse a significant architectural decision, record it as an ADR in `docs/adr/` (Nygard format; copy `docs/adr/template.md`). ADRs are immutable once accepted — supersede rather than edit. Existing records explain the `rich` choice, the 1 Hz refresh, and the no-plugins/no-config stance.
