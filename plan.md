# Harbor Console — service registry and tailnet status page

**Status: handoff brief, not an implementation plan.** Design work started
2026-09-01 in a session rooted in another repo; this document moves it here so
it can be finished and built in place. Each section below is marked
**APPROVED**, **PROPOSED**, or **OPEN**. Nothing here has been implemented.

Resume by finishing the OPEN sections, then writing a real implementation plan
into `docs/superpowers/plans/`.

---

## Why this exists

On 2026-09-01, ImageHarbor's face-recognition branch merged and was ready to
deploy. A probe of the target host found `hpz440:8080` already answering — with
a uvicorn app redirecting to `/login`. That is **GTE**, whose
`docker-compose.yml` publishes `8080:8080`.

ImageHarbor's `docker-compose.yml` publishes `8080:8080` as well.

The collision would not have announced itself. ImageHarbor's `serve()` catches
a bind failure, logs it, and deliberately keeps organizing without a dashboard
— "a dashboard failure must never stop the watcher." So the symptom would have
been a dashboard that simply never appeared, on a service that otherwise looked
healthy.

Two problems with one root cause:

1. **No authority over port assignment.** Nothing decides who owns 8080, and
   nothing checks before a second project claims it.
2. **No directory.** No single place says what is running, on which host and
   port, and whether it is up.

## What was rejected, and why

The original framing was mDNS/Zeroconf. **It was dropped**, and it should not
come back without new information:

- mDNS is link-local broadcast. **It does not cross a tailnet** — the case it
  was most wanted for is the case it cannot serve.
- Tailscale MagicDNS already resolves `hpz440` from anywhere on the tailnet, so
  name resolution was never the actual gap.
- The real gap is *which services exist, on what port, and are they up* — a
  directory question, not a discovery-protocol question.

A survey of the whole `programming/` tree found **no existing discovery or
reverse-proxy tooling** of your own: no zeroconf, avahi, traefik, caddy, or
nginx-proxy outside vendored third-party code. This is greenfield.

## Decisions locked

| Question | Decision |
| --- | --- |
| How do services get into the registry? | **Declared in one file, health-probed.** No changes required to any other project. |
| Scope | **One server now, schema carries `host` from day one** so a second machine is a data change, not a rewrite. |
| What does "gatekeeping containers" mean? | **Port allocation authority only.** Not lifecycle control, not access control. |
| Relationship to the existing terminal console | **Two entry points, one package.** Independent lifetimes. |

### Module layout (agreed)

```
src/harbor_console/
  system.py    collect metrics       (existing)
  registry.py  read + validate       (NEW)
  docker.py    live container state  (NEW)
  ui.py        render terminal       (existing)
  web.py       render + serve HTTP   (NEW)
  app.py       tty loop              (existing)
  webapp.py    systemd service       (NEW)
```

Two processes, one shared core. `harbor-console` stays the tty1 login-console
loop; `harbor-console-web` runs continuously under systemd. The lifetimes must
stay independent: logging in at the attached monitor must not take the tailnet
page down, and the tailnet page must not depend on anyone being logged in.

This fits the project's existing discipline rather than fighting it — the
`collect` / `render` / `coordinate` split with injected collaborators means a
web view is *a second renderer over the same collectors*.

## Current port map (measured 2026-09-01)

Read from each project's `docker-compose.yml`. This is the raw material for the
registry's first commit.

| Project | Published |
| --- | --- |
| GTE | 8080 |
| ImageHarbor | 8080 ← **collision** |
| FastAPI_Docker | 8000 |
| Retirement_planning | 8501, 8502 |
| My_River_level | 5743 |
| ice-colder | 26123, 1883 |

Suggested: ImageHarbor moves to 8081, `harbor-console-web` takes 8090 (nothing
claims it).

---

## Section 1 — the registry (APPROVED)

One file, `services.toml`, in this repo and deployed with it. Version-controlled
on purpose: it is an authority, so it should be diffable and reviewed, and
`deploy/install.sh` is already the idempotent update path.

```toml
[[service]]
name        = "gte"
title       = "GTE — General Triage Engine"
host        = "hpz440"        # present from day one; single-host behaviour today
port        = 8080            # published host port — this file owns it
container   = "gte"           # for reconciliation against docker
health_path = "/"             # probed; any HTTP response = up
description = "Business document triage console"
```

Three rules make it an authority rather than a list:

1. **`(host, port)` is unique**, validated when the file loads. A duplicate is a
   hard error, not a warning. That is the GTE/ImageHarbor collision caught at
   edit time.
2. **Every service is declared, including `harbor-console-web` itself.** It
   takes a port from the same file it serves.
3. **Declaring is not deploying.** The file records intent; Docker holds
   reality.

### Reconciliation

The page shows declared state beside live Docker state and names the three ways
they disagree:

- declared but not running
- running but not declared
- running on a port that does not match the declaration

The third is exactly today's failure mode.

### Health probing

Deliberately dumb: connect, and **any HTTP response means up**. GTE answers `/`
with a 303 redirect to `/login` — that is a healthy service, and a probe
insisting on 200 would call it down.

---

## Section 2 — the web surface (APPROVED 2026-09-01)

Approved and written into `founding_document.txt`; the binding rule is
[ADR 7](docs/adr/0007-bind-tailscale-address-only.md).

**Binding is the access control.** No login page; the gate is the network.
`harbor-console-web` binds to the host's Tailscale address **only**, never
`0.0.0.0`. If the Tailscale interface is not up yet it refuses to start and
systemd retries (`After=tailscaled.service`). A silent fallback to `0.0.0.0`
would publish an inventory of every service you run to the whole LAN — the one
mistake most worth designing against here.

**Stdlib `http.server`, one self-contained page.** No FastAPI, no uvicorn. This
project's runtime dependencies are `rich` and `psutil`; ImageHarbor's dashboard
already proves stdlib `http.server` is sufficient for exactly this job. Links
are built from the registry's `host` and `port` (`http://hpz440:8080`) —
MagicDNS resolves the name from anywhere on the tailnet, which is the whole
reason mDNS turned out to be unnecessary.

**Probing happens in the background, never in the request.** A prober thread
refreshes health on an interval; the page renders last-known results with a
timestamp. Serial probing inside the handler would mean one hung service makes
the status page take twenty seconds to load — the page that exists to tell you
something is wrong must not become the thing that is wrong.

**The page shows three things:** existing server health (hostname, uptime,
CPU/memory/disk, container count), the service directory with live/down state,
and the drift list from Section 1. Auto-refreshes on a timer.

**The page is read-only.** No buttons that do anything. Port authority was
chosen over lifecycle control, so nothing here can stop a container.

---

## Section 3 — enforcing the port authority (OPEN)

Never discussed. This was the next question. A file nobody consults is not an
authority, so this section decides what makes it binding. At least three
candidate mechanisms:

- **Advisory check** — a `harbor-console ports check` CLI, run by hand or in
  CI, that reads the registry and fails on duplicates. Nothing else changes.
- **Reconciliation display only** — the drift list is the enforcement; you see
  the mismatch on the page and fix it.
- **Generated configuration** — each project's compose reads a port from a file
  the registry emits, so drift is impossible by construction. Strongest
  guarantee, and the only option that couples other projects to this one.

Decide this before writing the implementation plan; it determines whether any
other repository is touched.

## Section 4 — remaining open items (OPEN)

- ~~**Superseding `founding_document.txt`.**~~ **DONE 2026-09-01.**
  `founding_document.txt` now carries a v0.2.0 section (registry + web surface);
  the v0.1.0 MVP section is preserved and relabelled as shipped, and its "no web
  server" line is explicitly superseded rather than deleted. Recorded as
  [ADR 6](docs/adr/0006-service-registry-and-web-status-page.md) (scope
  expansion, amending ADR 3's no-config stance) and
  [ADR 7](docs/adr/0007-bind-tailscale-address-only.md) (tailnet-only binding).
  `CLAUDE.md` and `docs/architecture.md` updated to match, with the v0.2.0
  modules marked as not yet implemented.
- **Testing strategy.** The existing suite drives `app.run()` with injected
  fakes and raises `KeyboardInterrupt` from a fake `sleep`. The web service and
  prober need an equivalent — no real sockets, no real time, no real Docker.
- **Second systemd unit.** `deploy/install.sh` currently installs one unit and
  masks `getty@tty1`. Adding `harbor-console-web.service` must keep the
  installer idempotent and must not touch the tty1 behaviour.
- **Registry file location on disk.** In-repo and rsynced to
  `/opt/harbor-console/services.toml` by the installer, or a config path under
  `/etc`? In-repo was assumed above; confirm it.

---

## Grounding facts (verified — do not re-derive)

- `deploy/install.sh` copies the repo to `/opt/harbor-console`, builds `.venv`
  with `uv sync`, creates a `harbor` system user, and installs a systemd unit.
  Idempotent; re-run to update.
- **The `harbor` user is already in the `docker` group**, so reading live
  container state needs no new permissions.
- The unit masks `getty@tty1` only; tty2–tty6 and SSH are unaffected. Runs with
  `ProtectHome=yes`.
- Runtime dependencies: `rich>=13.7`, `psutil>=5.9`. Python `>=3.13`. `uv` only
  — never pip or venv directly.
- `pyproject.toml` sets `pythonpath = ["src"]`; tests import `harbor_console`
  with no editable install.
- Architecture decisions live in `docs/adr/` (5 existing ADRs).
- Licence: MIT.

## Suggested next step

Finish Sections 2–4 as a design conversation in this repo, write the result to
`docs/superpowers/specs/YYYY-MM-DD-service-registry-design.md`, then produce an
implementation plan under `docs/superpowers/plans/`.
