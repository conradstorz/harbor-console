# harbor-console

A linux server attached monitor showing system health and projects status on every boot.

## Status page

`harbor-console-web` serves a read-only page to the tailnet answering the other
question: what is running on this host, where it is reachable, and is it up. It
shows the same health metrics as the attached monitor, a directory of every
container that declares itself with compose labels, and the findings where those
declarations and the host disagree — a container with no labels at all, one that
still publishes a raw host port instead of going through the proxy, a route
Traefik rejects, and a host process holding a tailnet port that no container
publishes.

Traefik is the only truth for which routes are live: the page reads Traefik's
API on loopback and Docker's labels, and probes each HTTP row at its own
`https://<name>.hpz440.ohr3023.org/`, so a green row proves the whole path
through the proxy rather than just a socket being open.

It binds the host's Tailscale address on port 8100 and nothing else. If that
address cannot be determined it refuses to start rather than falling back to a
broader one, because the page is an inventory of every service on the host and
binding *is* the access control — which is why there is no login page
([ADR 7](docs/adr/0007-bind-tailscale-address-only.md)). That is its only
startup refusal: Traefik or Docker being unreadable is a banner on the page and
a set of findings withheld, never a reason to stop serving. Probing runs in a
background thread, so one hung service cannot make the page slow to load, and
the page is strictly read-only: nothing on it can start or stop anything.

Health probing is deliberately dumb: any HTTP response means up, including a
redirect to a login page. A project can offer `/hcstatus` returning a little JSON
to add detail to its row; a missing or broken one never makes it show as down.

Run it with `uv run harbor-console-web`. `deploy/install.sh` installs it as a
second systemd unit alongside the tty1 dashboard; the two have independent
lifetimes, so restarting one does not disturb the other. Read it at
`https://harbor.hpz440.ohr3023.org/` from anywhere on the tailnet.

## The edge

Traefik fronts every HTTP service on the host, from `deploy/traefik/compose.yaml`
in this repository. It publishes the host's Tailscale address on `:80` and `:443`
and nothing else — the same bind-is-the-access-control rule as the page — and
holds one wildcard certificate for `*.hpz440.ohr3023.org`, issued by Let's
Encrypt over a Cloudflare DNS-01 challenge. Its API and dashboard are bound to
`127.0.0.1:8081`, where only the status page reads them.

Because the proxy terminates real TLS, a service that sets Secure cookies simply
works, and no HTTP service needs to publish a host port at all
([ADR 15](docs/adr/0015-reverse-proxy-and-label-declared-services.md)).

## Declaring a service

A service declares itself in its own compose file, with labels and a network,
and nowhere else. There is no registry in this repository to update, no sync
step, and nothing that generates the labels for you.

| Kind | Labels | `ports:` |
|---|---|---|
| HTTP | `traefik.enable=true`, ``traefik.http.routers.<name>.rule=Host(`<name>.hpz440.ohr3023.org`)``, and `traefik.http.services.<name>.loadbalancer.server.port=<port>` when the image exposes more than one port | none |
| TCP | `harbor.kind=tcp`, `harbor.port=<host port>` | keeps its entry |
| Internal | `harbor.kind=internal` | none |
| Edge | `harbor.kind=edge` (Traefik only) | 80 and 443 |

Any container may add `harbor.description=<one line>`, which the directory shows
next to its row.

Every routed container also needs `networks: [harbor]` and the top-level
`networks: { harbor: { external: true } }`. A container that is not on that
network is not routed, and Traefik says why — which the page reports as
`route-error`.

A container with none of these labels is reported as `undeclared-container`, and
one that publishes a host port no `harbor.kind=tcp` label accounts for is
reported as `bypasses-proxy`. Those two findings are the migration checklist: a
service either has a route or it is on the list.
