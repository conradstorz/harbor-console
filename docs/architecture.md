# Architecture

Harbor Console is two surfaces over one core, and the core is a strict split by
responsibility: **collect**, **render**, **coordinate**. Nothing does two of
those jobs.

## Shipped (v0.1.0)

- `system.py` collects all runtime metrics.
- `ui.py` only renders the dashboard from provided metrics.
- `app.py` runs a 1-second refresh loop and exits cleanly on `Ctrl+C`.

`harbor-console` runs this loop on tty1 under systemd.

## Specified, not yet implemented (v0.2.0)

A second process, `harbor-console-web`, serves a read-only status page to the
tailnet. It reuses the same collectors rather than duplicating them — which is
the payoff of the split above: a web view is a second renderer, not a second
application.

- `registry.py` collects: reads and validates `services.toml`, the declared
  authority on which service owns which `(host, port)`.
- `docker.py` collects: live container state, for reconciliation against the
  declaration.
- `web.py` renders: one self-contained HTML page, served over stdlib
  `http.server`.
- `webapp.py` coordinates: a background prober thread and the HTTP server.

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
