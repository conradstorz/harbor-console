# 15. A reverse proxy fronts every HTTP service; labels are the only declaration

Date: 2026-09-18

## Status

Accepted. Supersedes ADR 8, 9, 10, 11, 13 and 14; amends ADR 6, 7 and 12.

## Context

The lease ledger reserved host ports, but only for projects that declared a
`.harbor.toml` and ran `ports sync`. On 2026-09-18 a review found
`parksmart-parksmart-1` running on `127.0.0.1:8000` under the lease of the
not-running `fastapi-docker/api`; the page reported the dead lease as
LISTENING and no drift. Projects with no declaration never appeared.

Host ports were the scarce resource only because every service published
one. With a reverse proxy in front, no HTTP service publishes a port at all;
the scarce resource is the hostname, and the proxy knows every live one.

## Decision

- Traefik runs from `deploy/traefik/compose.yaml`, publishing
  `100.69.239.123:80` and `:443` only. The bind is still the access control
  (ADR 7); there is no `0.0.0.0` and no LAN exposure.
- One wildcard certificate for `*.hpz440.ohr3023.org` via Cloudflare
  DNS-01. Routes are `<name>.hpz440.ohr3023.org`.
- Containers declare themselves with compose labels and nothing else:
  `traefik.enable=true` plus a `Host()` rule for HTTP; `harbor.kind=tcp` and
  `harbor.port` for a TCP service that keeps a published port;
  `harbor.kind=internal` for one that publishes nothing; `harbor.kind=edge`
  for the proxy. Optional `harbor.description`.
- Traefik is the only truth for which routes are live. The page reads its
  API on loopback and Docker's labels, and reports: undeclared containers,
  containers that bypass the proxy with a raw port, routers Traefik rejects,
  and host processes on the tailnet no container publishes.
- The ledger, the allocator, `.harbor.toml`, `HARBOR_PORTS.md`, `/ports.json`
  and the `tailscale serve` collector are removed. The page binds fixed port
  8100 on the tailnet address and is reached at `harbor.hpz440.ohr3023.org`.

## Consequences

- A new service is one compose edit: labels and a network. No sync step, no
  file in this repo to update, no second declaration to drift.
- A service that still publishes a port is a finding, not a silent squat.
  The ParkSmart case cannot recur: it either has a route or it is reported.
- TLS is real everywhere, so Secure cookies work without `tailscale serve`.
- The proxy is a single point of failure for every HTTP service. It restarts
  with Docker and the page reports it as an edge row.
- Non-HTTP services are listed but not routed; MQTT stays on a published
  port by declaration.
- Probes now go through the proxy, so 502, 503 and 504 mean down while any
  other response — including the service's own 500 — means up. Only the edge
  produces those three on behalf of a backend that did not answer. This
  narrows ADR 12's "any HTTP response means up" for the proxied path only.
- A router Traefik does not report is a route error, not a down service: the
  container asked for a route and did not get one. That verdict is only
  available when Traefik answered; when it could not be asked, the probe
  decides, as it did before there was a proxy.
- The edge reaches the page through one ufw rule, scoped to the harbor bridge
  (`br-harbor`), the tailnet address and port 8100. ufw's default-deny INPUT
  drops container→host traffic, so without it Traefik's file-provider route
  times out. The installer creates the network with that fixed bridge name and
  refuses to continue against one that lacks it.
- ADRs 8–11, 13 and 14 describe machinery that no longer exists. They stay
  as the record of why it was built and what it caught.
