# Reverse proxy and label-declared services — design

Date: 2026-09-18
Status: approved in conversation, awaiting written review

## Why

The port ledger catches collisions on host ports, but only among projects that
declare a `.harbor.toml` and run `ports sync`. A review on 2026-09-18 found the
gap in practice: `parksmart-parksmart-1` was running on `127.0.0.1:8000` under
the lease held by the not-running `fastapi-docker/api`, the page reported the
dead lease as LISTENING, and reported no drift. New projects with no
declaration never appeared at all.

The decision is to leave the "reserve a host port" pattern for the "reverse
proxy in front, nothing publishes a port" pattern. Host ports stop being the
scarce resource; hostnames are, and Traefik knows every live one.

## Decisions

- **Traefik** is the proxy. Chosen over Caddy for built-in Docker label
  discovery and a routers API the page can read.
- **Cloudflare DNS-01** issues one wildcard certificate for
  `*.hpz440.ohr3023.org`. The DNS record `*.hpz440` A → `100.69.239.123`, proxy
  off. The address is tailnet-only, so the public record leads nowhere for
  anyone outside the tailnet.
- **Route names sit under the host**: `<name>.hpz440.ohr3023.org`. A second
  host later gets its own prefix.
- **Traefik is the only truth for what is routed.** The ledger, the allocator,
  and `.harbor.toml` retire. A container declares itself with compose labels
  and nothing else.
- **Labels are written directly in each compose file.** Nothing generates them.

## Section 1: the edge

Traefik runs as a container from `deploy/traefik/compose.yaml` in this repo.
harbor-console owns the host's edge the way it owned the ledger.

- Publishes `100.69.239.123:80` and `100.69.239.123:443`, nothing else. 80 only
  redirects to 443. A new ADR extends ADR 7 to the proxy: the bind is the
  access control, there is no `0.0.0.0`.
- One external Docker network, `harbor`, created once by the deploy script.
  Traefik's Docker provider is configured with `exposedByDefault=false` and
  `network=harbor`; a container that is not on the network is not routed.
- The Cloudflare API token lives in `/etc/traefik/env` on the host, mode 0600,
  never in the repo. Certificates persist in a named volume.
- Traefik's API and dashboard are enabled on an entrypoint bound to
  `127.0.0.1:8081` only. The page reads it from loopback.
- One file-provider route, `harbor.hpz440.ohr3023.org` → `http://100.69.239.123:8100`,
  for the page itself, because it is a systemd process and carries no labels.
- Traefik's own container carries `harbor.kind=edge`, a kind reserved for the
  proxy alone, so it is neither undeclared nor bypassing itself on 80 and 443.

### Declaration convention

| Kind | Labels | `ports:` |
|---|---|---|
| HTTP | `traefik.enable=true`, `traefik.http.routers.<name>.rule=Host(\`<name>.hpz440.ohr3023.org\`)`, and `traefik.http.services.<name>.loadbalancer.server.port=<port>` when the image exposes more than one port | none |
| TCP | `harbor.kind=tcp`, `harbor.port=<host port>` | keeps its entry |
| Internal | `harbor.kind=internal` | none |
| Edge | `harbor.kind=edge` (Traefik only) | 80 and 443 |

Any container may add `harbor.description=<one line>` for the directory.

Every routed container also needs `networks: [harbor]` and the top-level
`networks: { harbor: { external: true } }`.

### Cutover order on the host

1. Remove the two `tailscale serve` fronts (443 → portainer, 8443 → gte).
2. Deploy the page on tailnet:8100, dropping `CAP_NET_BIND_SERVICE` from the
   unit.
3. Create the `harbor` network, place the token, start Traefik.
4. Add the DNS record.
5. Migrate projects one at a time (section 3).

## Section 2: the page

Same collect / render / coordinate split. Fewer modules.

### Collectors

- `docker.py` — `docker inspect` over `docker ps -q`. `Container` carries
  `name`, `labels: dict[str, str]`, `published: tuple[(addr, port)]`, and
  `networks: frozenset[str]`. `DOCKER_UNAVAILABLE` stays, with the same
  meaning: could not ask, as opposed to nothing running. Timeout stays.
- `traefik.py`, new — `GET /api/http/routers` and `/api/http/services` on
  `127.0.0.1:8081`. Returns `Router(name, host, service, status, error)` where
  `host` is parsed from a `Host(...)` rule and `status` is Traefik's own
  `enabled`/`disabled`. Degrades to `TRAEFIK_UNAVAILABLE`, distinguishable
  from an empty router list the same way `DOCKER_UNAVAILABLE` is.
- `listening.py`, `tailnet.py` — unchanged.
- `probe.py` — probes HTTP rows through the proxy at
  `https://<host>/`, so a green row proves the whole path. `/hcstatus` for
  detail, by convention. Any HTTP response means up. TLS verification on;
  the certificate is real.

### Policy: `directory.py`

Pure, replacing `reconcile.py`. Containers, routers, listeners, tailnet address
in; rows and findings out.

Rows, one per declared container, ordered by name:

| Kind | Shows | State |
|---|---|---|
| http | URL | UP from probe, else DOWN; ROUTE ERROR if Traefik reports the router disabled |
| tcp | `addr:port` | LISTENING if a listener overlaps, else DOWN |
| internal | container name | INTERNAL |
| edge | `:80 :443` | LISTENING/DOWN like tcp |

Findings, in this order:

- `undeclared-container` — running, no `traefik.enable=true`, no `harbor.kind`.
- `bypasses-proxy` — publishes a host port that no `harbor.kind=tcp`/`edge`
  label accounts for. An HTTP service still on a raw port is this finding.
- `route-error` — a router Traefik reports disabled, with Traefik's error text.
  Covers a bad label and a routed container missing from the `harbor` network.
- `undeclared-tailnet-listener` — a host process binding the tailnet address
  exactly, no container publishes it, outside the ephemeral range. Kept from
  today, minus the `tailscale serve` logic.

All container-based findings are withheld when Docker is unavailable;
`route-error` is withheld when Traefik is unavailable. Absence of evidence is
reported as absence of evidence in the banner.

### Snapshot, render, coordinate

- `snapshot.py` — `leases` becomes `rows`; `traefik_available` joins
  `docker_available`; `probed` and `collection_error` unchanged.
- `web.py` — Host table, Directory table, Findings list, banner, stamp.
  `/ports.json` is removed; the allocator that read it is gone.
- `webapp.py` — one startup refusal: no tailnet address. Binds the tailnet
  address on fixed port 8100. Traefik or Docker being down is a banner, not a
  refusal. Prober stays a background thread, injected the same way.

### Retired

`ports/` package and the `ports` CLI, `services.toml`, `.harbor.toml`,
`HARBOR_PORTS.md`, `serve.py`, `addressing.py`, `reconcile.py`, the
`/ports.json` endpoint, the hourly sync task, and their tests. ADRs 8–14 are
superseded by one new ADR; ADR 7 is extended, not superseded. The tty
dashboard is untouched.

### Tests

Fake docker (`inspect` JSON fixtures), fake Traefik (router dicts), fake
sockets, injected sleep. No real time, no real network. `directory.py` is
tested with plain values. Each finding has a positive and a withheld case.

## Section 3: migrations

Each is one small commit in its own repository. The page's `bypasses-proxy`
list is the checklist; an unmigrated service stays reachable on its old port
until its turn.

| Container | Route name | Notes |
|---|---|---|
| harbor-console-web | harbor | file-provider route to tailnet:8100 |
| gte-admin-1 | gte | replaces the serve front on 8443 |
| parksmart-parksmart-1 | parksmart | |
| imageharbor-imageharbor-1 | images | |
| schminternet | schminternet | |
| my_river_level-app-1 | river | |
| ice-colder-vmc | ice | ice-colder-mqtt gets `harbor.kind=tcp`, `harbor.port=1883` |
| arm-rippers-dev | arm | |
| portainer | portainer | backend is its plain 9000 port; replaces the serve front on 443 |
| watchtower, shared-postgres-postgres-1, gte-db-1, ice-colder-sim-* | none | `harbor.kind=internal` |

`retirement-planning` and `fastapi-docker` are not running and have no
container to label. They leave the directory until deployed with labels.

Compose files for gte, arm, portainer, watchtower and shared-postgres are not
in the local programming folder; each is located on the server via the
container's `com.docker.compose.project.working_dir` label before it is edited.

## Out of scope

Authentication at the proxy, LAN exposure, a second host, TCP routing through
Traefik, generating labels from any file.
