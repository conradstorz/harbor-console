# Architecture

Harbor Console is two surfaces over one core, and the core is a strict split by
responsibility: **collect**, **render**, **coordinate**. Nothing does two of
those jobs.

## Shipped (v0.1.0)

- `system.py` collects all runtime metrics.
- `ui.py` only renders the dashboard from provided metrics.
- `app.py` runs a 1-second refresh loop and exits cleanly on `Ctrl+C`.

`harbor-console` runs this loop on tty1 under systemd.

## The edge

Traefik fronts every HTTP service on the host, from `deploy/traefik/compose.yaml`
in this repository. It publishes the host's Tailscale address on `:80` and `:443`
and nothing else, holds one wildcard certificate for `*.hpz440.ohr3023.org`, and
exposes its API and dashboard on `127.0.0.1:8081` only. Its Docker provider runs
with `exposedByDefault=false` on one external network, `harbor`: a container that
is not on that network is not routed.

That makes hostnames, not host ports, the scarce resource. An HTTP service
publishes no host port at all; it declares a `Host()` rule in its own compose
file and joins the network. The port ledger, the allocator and `.harbor.toml`
that this project used to reserve host ports are retired
([ADR 15](adr/0015-reverse-proxy-and-label-declared-services.md)); ADRs 8–11, 13
and 14 remain as the record of why they were built and what they caught.

## The tailnet status page

A second process, `harbor-console-web`, serves a read-only status page to the
tailnet. It reuses the same collectors rather than duplicating them — which is
the payoff of the split above: a web view is a second renderer, not a second
application.

Collect:

- `tailnet.py` — the host's Tailscale address, from `tailscale ip -4`. It is
  the one collector in the project permitted to **raise**: every other one
  degrades quietly, but a silent fallback here would publish an inventory of
  every service on the host to the whole LAN
  ([ADR 7](adr/0007-bind-tailscale-address-only.md)).
- `listening.py` — every listening TCP socket, from `psutil`. Only the host
  itself can see loopback-bound and non-Docker listeners. IPv6 `::` is
  normalised to `0.0.0.0`, and `addrs_overlap` — the rule that the wildcard
  contends with every address on its host while two specific addresses do not —
  lives here with it.
- `docker.py` — running containers: names, **labels**, published host ports and
  networks, from `docker inspect` over `docker ps -q`. Labels are how a
  container declares itself, which is why this asks `inspect` rather than
  `ps --format`. "Docker could not be asked" is a distinct result from "asked,
  nothing running": the difference decides whether the page may call a
  container undeclared.
- `traefik.py` — the routers Traefik reports, from its API on `127.0.0.1:8081`:
  the hostname parsed out of a `Host()` rule, the service behind it, and
  Traefik's own enabled/disabled verdict with its error text. It degrades to
  `TRAEFIK_UNAVAILABLE`, distinguishable from an empty router list exactly as
  `DOCKER_UNAVAILABLE` is.
- `probe.py` — liveness and optional detail for one **URL**, by convention: the
  route's own `https://<name>.hpz440.ohr3023.org/` for up, `/hcstatus` for
  detail. Any HTTP response means up
  ([ADR 12](adr/0012-web-surface-collectors-and-conventions.md)), and because
  the probe goes through the proxy, a green row proves the whole path.

Decide:

- `directory.py` — the policy, and pure: containers, routers, listeners and
  probe results in; rows and findings out. One row per declared container —
  `http`, `tcp`, `internal` or `edge` — and four findings:
  `undeclared-container` for a container carrying no declaration at all,
  `bypasses-proxy` for a published host port no `harbor.kind=tcp` label
  accounts for, `route-error` for a router Traefik rejects or never saw, and
  `undeclared-tailnet-listener` for a host process holding a tailnet port that
  no container publishes. Traefik is the truth for whether a route is live;
  this module only joins its verdict to the container that asked for it.

The contract:

- `snapshot.py` — data only, the handoff between the prober and the renderer.
  Its own module so neither side has to import the other.
  `Snapshot.probed` separates "collected, found nothing" from "collected
  nothing yet"; `docker_available` and `traefik_available` record which
  evidence the cycle actually had; and `Snapshot.collection_error` carries why
  a cycle failed, whatever its source — a collector or the prober itself. It is
  not named for any one source, because naming it that pointed every failure at
  the wrong place.

Render and coordinate:

- `web.py` renders: one self-contained HTML page from a snapshot, and serves it
  over stdlib `http.server`. It never collects and never probes, and it has no
  endpoint but the page.
- `webapp.py` coordinates: the background prober thread and the HTTP server, as
  the `harbor-console-web` systemd entry point.

Four properties of that service are structural rather than incidental:

- It binds the host's Tailscale address only, and **refuses to start** without
  it. There is no fallback, no `--host`, and no dev mode (ADR 7).
- That is its **only** startup refusal. It happens before anything is bound and
  exits non-zero with the reason on stderr, so systemd retries and the operator
  reads it in journald. (Port 8100 already in use fails the bind and exits the
  same way; that is the environment, not a second rule.)
- The port is **fixed at 8100**, a constant in `webapp.py` rather than
  configuration: Traefik's file-provider route for `harbor.hpz440.ohr3023.org`
  points at it, and nothing may move it out from under that route (ADR 15).
- Probing never runs inside a request handler, so one hung service cannot make
  the page slow to load. Missing evidence is reported, never guessed: until the
  first cycle completes the directory and findings read "unknown", and a cycle
  that could not reach Docker or Traefik raises a banner and withholds exactly
  the findings that evidence supported.

The two processes have independent lifetimes. Logging in at the attached
monitor must not take the tailnet page down, and the page must not depend on
anyone being logged in.

See [the reverse-proxy design](superpowers/specs/2026-09-18-reverse-proxy-design.md) for the design
this surface implements, and `founding_document.txt` for the original
specification of the dashboard.

## Why

The reasoning behind these and other choices is recorded as Architecture
Decision Records in [`adr/`](adr/README.md).
