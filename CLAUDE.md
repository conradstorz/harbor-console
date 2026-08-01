# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Harbor Console is a lightweight terminal dashboard that replaces the default Linux login console with an at-a-glance server health view (hostname, uptime, CPU/memory/disk, IPv4, Docker container count, clock). It refreshes once per second and exits cleanly on Ctrl+C. `founding_document.txt` is the authoritative spec — read it before adding features.

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

Strict three-way separation, one responsibility each — keep it this way:

- `system.py` — **collects** metrics only. `collect_system_metrics()` returns a flat `dict[str, str | float | int]`. No rendering.
- `ui.py` — **renders** only. `build_dashboard(metrics)` turns the metrics dict into a `rich` renderable. No business logic, no metric collection.
- `app.py` — **coordinates** the refresh loop (`rich.live.Live`). No collection or rendering logic of its own.

The dict returned by `collect_system_metrics()` is the contract between `system` and `ui`; its keys are asserted directly in `tests/test_system.py`. Changing a key means updating the collector, the renderer, and that test together.

### Dependency injection for testability

`app.run()` takes `collector`, `renderer`, and `sleep` as injectable parameters (defaulting to the real implementations). Tests drive the loop by passing fakes and raising `KeyboardInterrupt` from the fake `sleep` to exit after one iteration — no real time passes, no real metrics collected. Preserve this pattern when modifying the loop.

### Graceful degradation

Collectors never raise on a hostile environment: `get_docker_container_count()` returns `0` when the `docker` binary is missing or errors; `get_ipv4_address()` falls back to `127.0.0.1`. New collectors should follow suit — the dashboard "never crashes during normal operation" is a release criterion.

## Scope discipline

The project is deliberately minimal (MVP / YAGNI / KISS). `founding_document.txt` lists an explicit "Deferred Until Later" set — colors, themes, config files, plugins, interactive menus, service/Docker management, remote monitoring, and more. Do not add these without a demonstrated operational need. There are intentionally no config files, no colors, no keyboard shortcuts, no persistence, and no web server.

Development follows TDD and "main is always deployable."

When you make or reverse a significant architectural decision, record it as an ADR in `docs/adr/` (Nygard format; copy `docs/adr/template.md`). ADRs are immutable once accepted — supersede rather than edit. Existing records explain the `rich` choice, the 1 Hz refresh, and the no-plugins/no-config stance.
