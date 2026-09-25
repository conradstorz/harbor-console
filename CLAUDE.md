# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Harbor Console is a lightweight operational console for a small fleet of Linux servers, with two surfaces over one core, plus the host's edge:

- **`harbor-console`** (shipped, v0.1.0) — a terminal dashboard that replaces the default Linux login console with an at-a-glance server health view (hostname, uptime, CPU, storage — one row per local filesystem, plus volume-group slack, stray devices, remote mounts and an image-mount summary — memory and swap with their own scale, one row per GPU, IPv4, Docker container count, clock). Refreshes once per second, exits cleanly on Ctrl+C.
- **`harbor-console-web`** (shipped) — a read-only status page served to the tailnet: the directory of every container that declares itself with compose labels, its state as Traefik and a probe through Traefik report it, and the findings where the declarations and the host disagree. Runs as its own systemd unit (`deploy/harbor-console-web.service`) on the host's Tailscale address, fixed port 8100 ([ADR 7](docs/adr/0007-bind-tailscale-address-only.md)), and is read at `harbor.hpz440.ohr3023.org` through the proxy.
- **The edge** — Traefik, from `deploy/traefik/compose.yaml` in this repo, publishing the tailnet address's `:80` and `:443` and nothing else, with one wildcard certificate for `*.hpz440.ohr3023.org`. A container declares itself with labels and joins the `harbor` network; nothing generates those labels and there is no sync step ([ADR 15](docs/adr/0015-reverse-proxy-and-label-declared-services.md)).

`founding_document.txt` is the original spec and `plan.md` the dated handoff brief behind v0.2.0; both describe the port ledger and allocator that ADR 15 retired, so read them for reasoning and history, not for what exists. What exists is described by `docs/superpowers/specs/2026-09-18-reverse-proxy-design.md` and by this file.

## Commands

Uses `uv` (not pip/venv). Python 3.13+.

| Task | Command |
|------|---------|
| Install deps (incl. dev) | `uv sync --extra dev` |
| Run the dashboard | `uv run harbor-console` (or `uv run python -m harbor_console`) |
| Run all tests | `uv run pytest` |
| Run one test file | `uv run pytest tests/test_system.py` |
| Run one test | `uv run pytest tests/test_system.py::test_format_uptime` |
| Run the tailnet status page | `uv run harbor-console-web` (binds the Tailscale address on port 8100; refuses without one) |

`pyproject.toml` sets `pythonpath = ["src"]`, so tests import `harbor_console` without an editable install.

## Architecture

Strict separation by responsibility — collect, render, coordinate — one job each. Keep it this way; it is what makes a second surface cheap.

Existing (implemented):

- `system.py` — **collects** metrics only. `collect_system_metrics()` returns a flat `dict[str, str | float | int]`. No rendering.
- `storage.py` — **collects** what this host's storage holds, from two sources: `psutil` for every mounted local filesystem, `lsblk` for the block layer underneath it — the volume-group slack `df` cannot see, and devices nothing has mounted. An entry carries numbers where they exist and a reason where they do not, so a renderer never has to decide which case it is looking at; a network mount is named from `/proc/mounts` but never measured, because `statvfs` on a dead CIFS mount blocks, and blocking is fatal to a 1 Hz refresh loop and to the web prober alike. Shared by both surfaces — `app.py` imports it exactly like `system.py`, and `webapp.py` imports it exactly like the web-surface collectors below.
- `gpu.py` — **collects** what GPUs this host has, from `/sys/class/drm`: one entry per `card<N>`, with the driver name from `device/uevent` and, where the driver exposes them, busy percent, VRAM used/total and hwmon temperature. Every metric is optional and each file is guarded on its own, so the `radeon` driver's one number (temperature) and `amdgpu`'s five render through the same `format_gpu`. Reads files and runs no subprocess, so nothing can hang. An unlistable root is one `unavailable` entry; no cards is an empty tuple, which both renderers state outright (`none detected` on the terminal, `No GPU detected.` on the page) rather than leaving a blank. Shared by both surfaces like `storage.py`.
- `ui.py` — **renders** only. `build_dashboard(metrics)` turns the metrics dict into a `rich` renderable. No business logic, no metric collection.
- `app.py` — **coordinates** the refresh loop (`rich.live.Live`). No collection or rendering logic of its own.

The web surface is ten modules at the top level, and keeps the same split:

- `tailnet.py` — **collects** the host's Tailscale address from `tailscale ip -4`. The one collector allowed to raise (see Graceful degradation below).
- `listening.py` — **collects** every listening TCP socket and every bound UDP socket via `psutil`, including loopback-bound and non-Docker ones. UDP has no `LISTEN` state, so a datagram socket counts when it has no peer; `Listener.proto` tells the two apart, and the policy in `directory.py` requires `tcp` wherever it checks a TCP service's liveness. IPv6 `::` is normalised to `0.0.0.0`, and `addrs_overlap` lives here: the wildcard contends with every address on its host, two specific addresses do not contend. Degrades to `LISTENING_UNAVAILABLE` when the socket table itself could not be read, distinguishable from a host with nothing listening the same way `DOCKER_UNAVAILABLE` is -- the page always binds a socket of its own, so a genuinely empty reachable inventory never happens.
- `docker.py` — **collects** running containers: names, **labels**, published host ports and networks, via `docker inspect` over `docker ps -q`. Labels are how a container declares itself, which is why this reads `inspect` rather than `ps --format`. `DOCKER_UNAVAILABLE` distinguishes "could not ask Docker" from "asked, nothing running"; the difference decides whether the page may call a container undeclared.
- `traefik.py` — **collects** the routers Traefik reports from its API on `127.0.0.1:8081`: the hostname parsed out of a `Host()` rule, the service behind it, and Traefik's own enabled/disabled verdict with its error text. Degrades to `TRAEFIK_UNAVAILABLE`, which is distinguishable from an empty router list the same way `DOCKER_UNAVAILABLE` is.
- `probe.py` — **collects** liveness and optional detail for one **URL**: the route's own `https://<name>.hpz440.ohr3023.org/` for up, `/hcstatus` for detail, both by convention ([ADR 12](docs/adr/0012-web-surface-collectors-and-conventions.md)). Any HTTP response means up, so a green row proves the whole path through the proxy.
- `directory.py` — the policy. Pure: containers, routers, listeners and probe results in; rows and findings out, so every rule is testable with plain values. Traefik is the truth for whether a route is live; this module only joins its verdict to the container that asked for it. Four findings — `undeclared-container`, `bypasses-proxy`, `route-error`, `undeclared-tailnet-listener` — each withheld when the evidence it needs is missing: container findings without Docker, route findings without Traefik, `undeclared-tailnet-listener` without the socket table. Absence of evidence is never a finding. `undeclared-tailnet-listener` counts a wildcard bind as reaching the tailnet, because `0.0.0.0` answers there too.
- `inventory.py` — the other policy, also pure: listeners and containers in, one entry per socket out, carrying who can reach it (`loopback`, `tailnet`, `LAN`, `LAN + tailnet`) and what accounts for it (a container, this page, a PID, or nothing). Where `directory.py` reports the declared services, this reports what is listening at all — the question a finding rule cannot be trusted with, because a filter applied before anyone can look is unfalsifiable by inspection, which is how a wildcard bind stayed hidden from `undeclared-tailnet-listener` ([ADR 18](docs/adr/0018-show-the-full-listening-inventory.md)). UDP appears here and generates no findings.
- `snapshot.py` — the **contract** between prober and renderer, data only. Its own module so neither imports the other. `Snapshot.probed` separates "found nothing" from "not looked yet"; `docker_available`, `traefik_available`, and `listeners_available` carry which evidence the cycle had; `Snapshot.collection_error` carries why a cycle failed, whatever its source.
- `web.py` — **renders** the HTML page from a snapshot and serves it over stdlib `http.server`. No collection, no probing, and no endpoint but the page itself.
- `webapp.py` — **coordinates**: the background prober thread and the HTTP server, as the `harbor-console-web` systemd entry point.

Four behaviours of that service are load-bearing and easy to undo by accident:

- **The page binds the host's Tailscale address on fixed port 8100.** The port is a constant in `webapp.py`, not configuration: Traefik's file-provider route for `harbor.hpz440.ohr3023.org` points at it, and there is deliberately no flag, no environment variable and no file that moves it ([ADR 15](docs/adr/0015-reverse-proxy-and-label-declared-services.md)).
- **`harbor-console-web` has one startup refusal, and only one: no tailnet address.** It happens before anything is bound, exits non-zero, and leaves the reason in journald for systemd to retry against. The four refusals of the ledger era went with the ledger. (Port 8100 already in use fails the bind and exits the same way, but that is the environment rather than a second rule.)
- **Traefik, Docker, or the host's own socket table being unreadable is a banner, never a refusal.** The page still renders from what it does have, says in the banner which evidence is missing, and withholds exactly the findings and row states that evidence supported -- a TCP or edge row whose listeners could not be read says `UNKNOWN`, not `DOWN`. A cycle that fails outright leaves the last good snapshot standing with the reason attached, and a successful cycle clears it.
- **Both units set `ProtectHome=read-only`, not `ProtectHome=yes`.** The
  storage collector reports the mounts the *service's* namespace has, not the
  host's. `yes` replaces `/home` with an empty tmpfs, which on hpz440 silently
  hid `/home/arm/media` -- 2.4 TiB, the largest filesystem on the machine and
  the reason `storage.py` exists. Nothing failed; the row simply was not there,
  because from inside the unit the volume was not mounted. `read-only` is the
  weakest setting that still shows it, and all this process needs: it calls
  `statvfs` and never writes to `/home`. `PrivateTmp=yes` stays; its private
  `/tmp` and `/var/tmp` are real mounts the process really has, but they are
  bind views of the same root volume as `/`, `/home` and `/root` -- five
  mountpoints for one device, and the kernel can even list one of them twice.
  `local_filesystems` groups by backing device and keeps one row per volume,
  at its shortest mountpoint, so `/tmp` and `/var/tmp` collapse into the `/`
  row instead of repeating it. That is not the kind of pre-emptive filter
  [ADR 18](docs/adr/0018-show-the-full-listening-inventory.md) argues
  against -- every distinct volume still gets exactly one row; what is gone
  is repeated views of bytes already counted, not a mount dropped because of
  what it is.

The two processes share the core and have independent lifetimes — logging in at tty1 must not take the web page down, and vice versa. The web view is a second renderer over the same collectors, not a second application.

The dict returned by `collect_system_metrics()` is the contract between `system` and `ui`; its keys are asserted directly in `tests/test_system.py`. Changing a key means updating the collector, the renderer, and that test together.

### Declaring a service

A container declares itself in its own compose file and nowhere else. Nothing in this repository generates these labels, and no file here has to be updated when a service appears.

| Kind | Labels | `ports:` |
|---|---|---|
| HTTP | `traefik.enable=true`, ``traefik.http.routers.<name>.rule=Host(`<name>.hpz440.ohr3023.org`)``, and `traefik.http.services.<name>.loadbalancer.server.port=<port>` when the image exposes more than one port | none |
| TCP | `harbor.kind=tcp`, `harbor.port=<host port>` | keeps its entry |
| Internal | `harbor.kind=internal` | none |
| Edge | `harbor.kind=edge` (Traefik only) | 80 and 443 |

Any container may add `harbor.description=<one line>` for the directory.

Every routed container also needs `networks: [harbor]` and the top-level `networks: { harbor: { external: true } }`. A container that is not on that network is not routed, and Traefik says so — which the page reports as `route-error`.

### Dependency injection for testability

`app.run()` takes `collector`, `renderer`, and `sleep` as injectable parameters (defaulting to the real implementations). Tests drive the loop by passing fakes and raising `KeyboardInterrupt` from the fake `sleep` to exit after one iteration — no real time passes, no real metrics collected. Preserve this pattern when modifying the loop, and keep it in the web service: the prober and HTTP server get the same treatment, with no real sockets, no real time, no real Docker and no real Traefik in tests.

### Graceful degradation

Collectors never raise on a hostile environment: `get_docker_container_count()` returns `0` when the `docker` binary is missing or errors; `get_ipv4_address()` falls back to `127.0.0.1`; `docker.py` and `traefik.py` return their own sentinels rather than an exception or a silent empty list. `storage.py`'s `local_filesystems`, `remote_mounts`, and `block_devices` each emit their own sentinel entry the same way — `NOTE_UNAVAILABLE`, or `NOTE_PERMISSION_DENIED` for a mount that exists but the process cannot read (the running `User=harbor` cannot read `/home/arm/media` on the one host this ships to, a normal condition here, not a hostile one) — rather than raising or silently dropping a row, the same "absence of evidence is never a finding" rule ADR 18 states for the listening inventory. New collectors should follow suit — the dashboard "never crashes during normal operation" is a release criterion.

A `NOTE_PERMISSION_DENIED` row still usually carries a size, and that fallback is the part most likely to get silently refactored away by someone who sees the note but not why a number sits next to it: `local_filesystems` takes a `sizes` mapping, and `collect_storage`'s default wires it to `mounted_device_sizes()`, which reads the same `lsblk` output `block_devices` does. The console runs unprivileged (ADR 5) and cannot `statvfs` a locked-down mount, but `lsblk`'s view of the block layer underneath is not privilege-gated, so a mount `disk_usage` refuses can usually still report the size the block layer already knows.

One deliberate exception, where failing loudly is the point:

- `harbor-console-web` refuses to start without a tailnet address. There is no fallback to `0.0.0.0` — see the hard constraints below. `tailnet.py` is therefore the one collector that raises rather than degrading.

## Hard constraints

These are load-bearing decisions, not preferences. Changing one needs a new ADR.

- **The web service binds the Tailscale address only, never `0.0.0.0`, with no override and no dev-mode relaxation.** The page is an inventory of every service on the host; a silent broader bind publishes it to the whole LAN. Binding *is* the access control, which is why there is no login page ([ADR 7](docs/adr/0007-bind-tailscale-address-only.md)).
- **Traefik publishes the tailnet address only, and its API is loopback only.** `100.69.239.123:80` and `:443` are the whole of the edge's exposure; the API and dashboard live on `127.0.0.1:8081`, where only the page reads them. The bind is the access control for the proxy exactly as it is for the page ([ADR 15](docs/adr/0015-reverse-proxy-and-label-declared-services.md) extends ADR 7).
- **Stdlib `http.server` only.** The web surface adds no runtime dependency; FastAPI and uvicorn were rejected as disproportionate to one page.
- **Probing happens in a background thread, never inside a request handler.** One hung service must not make the status page slow to load.
- **The page is read-only.** Its authority is reporting — not container lifecycle, not access control, not routing. No buttons that do anything.
- **Health probing is dumb on purpose: any HTTP response means up, except the three the proxy invents.** GTE answers `/` with a 303 to `/login`; a probe insisting on 200 would call a healthy service down. Probes now go through Traefik, so 502/503/504 are the edge answering for a backend that did not and mean down; a service's own 500 is still a service answering ([ADR 15](docs/adr/0015-reverse-proxy-and-label-declared-services.md) narrows [ADR 12](docs/adr/0012-web-surface-collectors-and-conventions.md) for the proxied path only).

## Scope discipline

The project is deliberately minimal (MVP / YAGNI / KISS). `founding_document.txt` lists an explicit "Deferred Until Later" set — colors, themes, plugins, user configuration, interactive menus, service/Docker management, notifications, multi-host aggregation, and more. Do not add these without a demonstrated operational need. There are intentionally no colors, no keyboard shortcuts, no persistence, and no user-facing configuration.

The scope has expanded twice, and each time the bar was a failure that had already happened: a real collision on port 8080 that failed silently ([ADR 6](docs/adr/0006-service-registry-and-web-status-page.md) amends [ADR 3](docs/adr/0003-no-plugins-in-mvp.md)), and then a container squatting a lease held by a service that was not running, which the ledger could not see because that project never declared anything ([ADR 15](docs/adr/0015-reverse-proxy-and-label-declared-services.md)). The no-config stance still holds: a service's declaration lives in its own compose file, and this repo keeps no registry of them.

ADR 15 is where the port ledger, the allocator, `.harbor.toml`, `HARBOR_PORTS.md` and `/ports.json` went, and why. ADRs 8–11, 13 and 14 describe that machinery; they stay as the record of why it was built and what it caught, not as a description of the code. Authentication at the proxy, LAN exposure, a second host, TCP routing through Traefik, and generating labels from any file are all out of scope.

Development follows TDD and "main is always deployable."

When you make or reverse a significant architectural decision, record it as an ADR in `docs/adr/` (Nygard format; copy `docs/adr/template.md`). ADRs are immutable once accepted — supersede rather than edit. Existing records explain the `rich` choice, the 1 Hz refresh, the no-plugins/no-config stance, and the reverse proxy.
