# 6. Expand scope to a service registry and a tailnet status page

Date: 2026-09-01

## Status

Accepted — supersedes the "no web server" constraint of the v0.1.0 MVP and
amends [ADR 3](0003-no-plugins-in-mvp.md) with respect to declared data files.
The `(host, port)` uniqueness rule below is superseded by
[ADR 10](0010-address-scoped-port-key.md), which scopes the key to the bind
address: `(host, addr, port)`. Everything else here stands.

## Context

`founding_document.txt` scoped this project to "a lightweight terminal
dashboard" answering one question — "is my server healthy?" — and explicitly
excluded a web server. [ADR 3](0003-no-plugins-in-mvp.md) further excluded
configuration files, and recorded that adding a deferred capability later
"should be recorded as a new ADR that supersedes or amends this one, so the
reason for the reversal is captured." This is that ADR.

On 2026-09-01 an operational need was demonstrated. ImageHarbor was ready to
deploy to a host where GTE was already serving on port 8080; both projects
publish `8080:8080`. ImageHarbor deliberately treats a dashboard bind failure
as non-fatal — "a dashboard failure must never stop the watcher" — so the
collision would have produced no error anyone would see, only a dashboard that
never appeared on an otherwise healthy service.

Two gaps with one root cause:

1. Nothing decides who owns a port, and nothing checks before a second project
   claims one.
2. Nothing says what is running, on which host and port, and whether it is up.

mDNS/Zeroconf was the original framing and was rejected: it is link-local
broadcast and does not cross a tailnet, which is exactly where the answer was
wanted. Tailscale MagicDNS already resolves host names fleet-wide, so name
resolution was never the gap. The gap is a directory, not a discovery protocol.
A survey of the whole `programming/` tree found no existing discovery or
reverse-proxy tooling to build on; this is greenfield.

## Decision

We will expand the project's scope from one terminal dashboard to two surfaces
over one core:

- `harbor-console` keeps tty1, unchanged.
- `harbor-console-web` runs continuously under systemd and serves a read-only
  status page to the tailnet. The two have independent lifetimes.

We will add a version-controlled `services.toml` registry that declares every
service — name, title, host, port, container, health path, description — and
enforce three rules: `(host, port)` is unique and a duplicate is a hard error
at load time; every service is declared, including `harbor-console-web` itself;
and declaring is not deploying, so the page reconciles the declaration against
live Docker state and names the three ways they can disagree.

Health probing is deliberately dumb: any HTTP response means up. Probing runs
in a background thread, never inside a request.

The registry is data owned by this repository, not user configuration of the
dashboard, so ADR 3's no-config stance still holds for its original target:
there are still no themes, no plugins, no user-tunable dashboard settings.

The registry's authority is limited to port allocation — not container
lifecycle, and not access control. Nothing on the page can start or stop
anything.

The mechanism that makes the registry *binding* (advisory CLI check,
reconciliation display only, or generated compose configuration) is **not
decided here**. It is recorded as an open question in `founding_document.txt`
and needs its own ADR, because only one of those options couples other
repositories to this one.

## Consequences

- The 8080 class of failure is caught when the file is edited rather than when
  a deployment silently half-works.
- No other project has to change to appear in the registry. Declaring is a
  local edit here.
- Module count grows from three to seven (`registry`, `docker`, `web`,
  `webapp` join `system`, `ui`, `app`). The collect / render / coordinate split
  is what makes this cheap: the web view is a second renderer over the same
  collectors, not a second application.
- Two systemd units instead of one. `deploy/install.sh` must stay idempotent
  and must not disturb the tty1 behaviour when adding the second.
- Runtime dependencies do not grow. `tomllib` and `http.server` are standard
  library; FastAPI and uvicorn were rejected as disproportionate to one page.
- The registry can lie. It records intent, and reality lives in Docker — which
  is precisely why reconciliation is a feature and not an afterthought.
- Testing gets a new problem: the prober and the HTTP server need the same
  injected-fakes treatment `app.run()` already has, with no real sockets, no
  real time, and no real Docker.
