# How Docker, Cloudflare, and Traefik work together on hpz440

This document explains, in detail, the reverse-proxy setup introduced by
[ADR 15](adr/0015-reverse-proxy-and-label-declared-services.md) and built by
`deploy/traefik/compose.yaml` and `deploy/install.sh`. It is written for
someone who understands Docker in general but has not set up Traefik or
Let's Encrypt DNS-01 before, and who will eventually need to debug this on a
2am pager alert. It documents what is actually running on hpz440 as of the
2026-09-19 cutover, not a generic tutorial.

If you only need to declare a new service, see "Declaring a service" in
[CLAUDE.md](../CLAUDE.md) — a label table is enough for that. Read this
document when something is broken, when you're changing the edge itself, or
when you want to understand *why* it's built this way.

## The problem this replaced

Before this, every container that wanted to be reachable published a host
port (`ports: ["8080:8080"]` in its compose file), and a hand-maintained
ledger (`services.toml`, now deleted) tracked which project owned which port.
That worked until a container started on a port the ledger had leased to a
*different*, non-running project — Docker doesn't know or care what a
ledger thinks a port means, it just binds whatever the compose file asks for.
That happened in practice: `parksmart-parksmart-1` silently squatted on
`127.0.0.1:8000`, a port the ledger believed belonged to a dead project called
`fastapi-docker`, and nothing caught it for days.

The fix is to stop publishing host ports for HTTP services at all. Only one
process on the whole host binds a public-facing port: Traefik, on 80 and 443.
Every other HTTP container sits on a private Docker network with **no**
published port, and Traefik routes to it by container name over that private
network. There is no port to collide on, because there is no port.

## The three layers, and how they compose

```
Browser / curl on the tailnet
        │  https://parksmart.hpz440.ohr3023.org/
        ▼
┌─────────────────────────────────────────────┐
│  Cloudflare DNS (authoritative for ohr3023.org)
│  *.hpz440.ohr3023.org  A  100.69.239.123      │   <- just a DNS answer,
│  (DNS-only: NOT proxied through Cloudflare's   │      Cloudflare's edge
│   network — see "Why DNS-only" below)          │      servers are never
└─────────────────────────────────────────────┘      in the path
        │  resolves to the host's own Tailscale address
        ▼
┌─────────────────────────────────────────────┐
│  hpz440, on the tailnet (100.69.239.123)       │
│  ┌─────────────────────────────────────────┐  │
│  │ Traefik container                        │  │
│  │  binds 100.69.239.123:443 (TLS)          │  │
│  │  binds 100.69.239.123:80  (redirect)     │  │
│  │  binds 127.0.0.1:8081     (API, no TLS)  │  │
│  │                                           │  │
│  │  reads: Docker socket (which containers  │  │
│  │         exist, what labels they carry)   │  │
│  │  reads: /etc/traefik/dynamic/*.yml       │  │
│  │         (routes for non-container        │  │
│  │         backends, i.e. harbor-console)   │  │
│  └───────────────┬───────────────────────────┘  │
│                  │ picks a router by Host()      │
│                  │ header, forwards over the      │
│                  │ "harbor" Docker network        │
│                  ▼                                │
│  ┌─────────────────────────────────────────┐    │
│  │ parksmart-parksmart-1 (no published port) │   │
│  │  joined to the "harbor" network            │   │
│  │  listens on :8000 inside that network only │   │
│  └─────────────────────────────────────────┘    │
└─────────────────────────────────────────────┘
```

Three independent systems, each doing one job:

1. **Cloudflare DNS** answers "what IP address is `parksmart.hpz440.ohr3023.org`?"
   with the host's Tailscale address, and nothing else. It does not proxy
   traffic, does not know Traefik exists, does not know what a container is.
2. **Traefik** is the only process that binds a public port. It decides,
   per request, which container should answer, based on the `Host` header of
   the HTTPS request and the labels on running containers. It also handles
   the TLS handshake and the certificate.
3. **Docker networking** (the `harbor` bridge network) is what actually
   carries the forwarded request from Traefik's container to the target
   container. Nothing here involves the host's own network stack once the
   request is inside Traefik.

## Layer 1: Cloudflare DNS

### The record

One DNS record does all the work:

| Type | Name | Content | Proxy status | TTL |
|---|---|---|---|---|
| A | `*.hpz440` | `100.69.239.123` | DNS only (grey cloud) | Auto |

This is a **wildcard** record under the subdomain `hpz440.ohr3023.org`. It
means *every* hostname ending in `.hpz440.ohr3023.org` — `parksmart.`,
`river.`, `anything-i-havent-thought-of-yet.` — resolves to the same IP
address, `100.69.239.123`, which is this host's Tailscale address. Adding a
new service never requires a new DNS record; the wildcard already covers it.
Only the apex `hpz440.ohr3023.org` itself and any name in a *different*
subdomain would need their own record (there aren't any).

`100.69.239.123` is not a public IP. It's an address in Tailscale's
`100.64.0.0/10` CGNAT range, only routable between devices on the same
tailnet. Publishing it in public DNS does not expose anything: anyone not on
this tailnet who resolves `parksmart.hpz440.ohr3023.org` gets an address they
have no route to. The DNS record is effectively a convenience — human-typeable
hostnames and free TLS certificates — layered on top of an already-private
network, not the thing providing the privacy. **The privacy comes entirely
from Tailscale routing and from Traefik binding that address specifically**
(more on that in Layer 2 and ADR 7).

### Why "DNS only" and not "Proxied"

Cloudflare's orange-cloud "Proxied" mode routes traffic through Cloudflare's
own edge network — the visitor's browser connects to a Cloudflare server,
which then connects onward to the address in the DNS record. That is
completely wrong here for two reasons:

1. Cloudflare's edge servers live on the public internet. They have no route
   to a `100.64.0.0/10` Tailscale address — the connection to the origin
   would simply fail.
2. Even if it worked, it would mean every request to a "private" service on
   this tailnet physically transited Cloudflare's infrastructure, defeating
   the entire point of keeping it tailnet-only.

"DNS only" (grey cloud) makes Cloudflare answer strictly as a DNS server: it
tells you the IP address and gets out of the way. The actual TCP/TLS
connection goes directly from the client to `100.69.239.123`, which only
works at all because the client is on the tailnet.

### The API token and DNS-01

Let's Encrypt (the certificate authority Traefik uses) offers several ways to
prove you control a domain before it will issue you a certificate. The
relevant ones here:

- **HTTP-01**: Let's Encrypt makes an HTTP request to `http://<domain>/.well-known/acme-challenge/<token>` and expects a specific response. This requires the domain to be reachable *from the public internet* on port 80. Ours isn't — it's tailnet-only. HTTP-01 is a non-starter for this host.
- **DNS-01**: Let's Encrypt asks you to create a specific `TXT` record (`_acme-challenge.<domain>`) with a specific value, then checks that the record exists via public DNS lookups. This works even for a domain with no public IP at all, because it only ever queries DNS (which is public, even though the address it points to isn't reachable). This is the only option that fits, and it's also the only way to get a **wildcard** certificate (`*.hpz440.ohr3023.org`) — Let's Encrypt requires DNS-01 for wildcards, full stop, no exceptions.

So Traefik needs to be able to create and delete `TXT` records under
`ohr3023.org` on Cloudflare, automatically, every time it renews. That's
what the API token is for:

- Created in the Cloudflare dashboard under **My Profile → API Tokens →
  Create Token**, using the **"Edit zone DNS"** template.
- Scoped to **Zone / DNS / Edit**, restricted to the single zone
  `ohr3023.org`. It cannot touch any other zone in the Cloudflare account,
  and it cannot do anything other than manage DNS records (it can't, for
  example, change the proxy status of unrelated records, purge cache, or
  see billing).
- Stored in `/etc/traefik/env` on the host as `CF_DNS_API_TOKEN=<token>`,
  mode `0600`, owned by root. This file is deliberately **not** in the git
  repository — see "Secrets" below.

### What actually happens on renewal

1. Traefik's ACME client asks Let's Encrypt for a certificate covering
   `hpz440.ohr3023.org` and `*.hpz440.ohr3023.org` (see the `tls.domains[0]`
   config below).
2. Let's Encrypt returns a challenge: "create `_acme-challenge.hpz440.ohr3023.org`
   as a `TXT` record with this exact value."
3. Traefik's Cloudflare provider (the `lego` library it embeds) calls the
   Cloudflare API with the token to create that `TXT` record.
4. Traefik polls Let's Encrypt's own recommended resolver
   (`--certificatesresolvers.cloudflare.acme.dnschallenge.resolvers=1.1.1.1:53`
   in our config) until the record is visible, to avoid asking Let's Encrypt
   to check before Cloudflare's own DNS has propagated the change.
5. Traefik tells Let's Encrypt "the record is up, please check now."
   Let's Encrypt does its own independent DNS lookup (from its own servers,
   not through our resolver) and, if it matches, issues the certificate.
6. Traefik deletes the `TXT` record (it was only needed for the proof) and
   stores the issued certificate in `/letsencrypt/acme.json` inside the
   named Docker volume `letsencrypt`, so it survives container restarts.
7. Traefik automatically renews roughly a month before expiry, repeating
   this whole dance without anyone touching anything.

This is why there is no `caserver` override in the compose file pinning
Traefik to Let's Encrypt's staging environment: the first-ever run goes
straight at the **production** ACME server. Production has stricter rate
limits (roughly 5 failed authorizations per account per hostname per hour),
so it's worth verifying the token works *before* starting Traefik for the
first time — see "Verifying the token" in Troubleshooting.

## Layer 2: Traefik

### What Traefik actually is

Traefik is a reverse proxy and load balancer that is unusual in one specific
way: instead of you hand-writing a config file that lists every route, you
tell it *where to look for routes* (a "provider"), and it watches that
source continuously and updates its routing table live, with no restart.
We use two providers:

- **Docker provider**: watches the Docker socket for containers with
  specific labels, and builds routes from those labels automatically.
- **File provider**: watches a directory of YAML files for routes to
  backends that *aren't* Docker containers — in our case, the harbor-console
  page itself, which is a plain systemd process, not a container.

### The compose file, annotated

This is `deploy/traefik/compose.yaml` as deployed. Every flag is explained
below in the order it appears.

```yaml
services:
  traefik:
    image: traefik:v3.3
    container_name: traefik
    restart: unless-stopped
    command:
      - --providers.docker=true
      - --providers.docker.exposedbydefault=false
      - --providers.docker.network=harbor
      - --providers.file.directory=/etc/traefik/dynamic
      - --providers.file.watch=true
      - --entrypoints.web.address=:80
      - --entrypoints.web.http.redirections.entrypoint.to=websecure
      - --entrypoints.web.http.redirections.entrypoint.scheme=https
      - --entrypoints.websecure.address=:443
      - --entrypoints.websecure.http.tls=true
      - --entrypoints.websecure.http.tls.certresolver=cloudflare
      - --entrypoints.websecure.http.tls.domains[0].main=hpz440.ohr3023.org
      - --entrypoints.websecure.http.tls.domains[0].sans=*.hpz440.ohr3023.org
      - --entrypoints.traefik.address=:8081
      - --api.insecure=true
      - --certificatesresolvers.cloudflare.acme.email=${ACME_EMAIL:?...}
      - --certificatesresolvers.cloudflare.acme.storage=/letsencrypt/acme.json
      - --certificatesresolvers.cloudflare.acme.dnschallenge=true
      - --certificatesresolvers.cloudflare.acme.dnschallenge.provider=cloudflare
      - --certificatesresolvers.cloudflare.acme.dnschallenge.resolvers=1.1.1.1:53
      - --log.level=INFO
    ports:
      - "${TAILNET_ADDRESS:?...}:80:80"
      - "${TAILNET_ADDRESS:?...}:443:443"
      - "127.0.0.1:8081:8081"
    env_file:
      - /etc/traefik/env
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./dynamic:/etc/traefik/dynamic:ro
      - letsencrypt:/letsencrypt
    networks:
      - harbor
    labels:
      harbor.kind: edge
```

**`--providers.docker=true`** — turn on the Docker provider at all.

**`--providers.docker.exposedbydefault=false`** — the single most important
security-relevant flag in this file. By default, Traefik's Docker provider
would create a route for **every** running container automatically, using
its container name as the hostname, whether or not that container wanted to
be public. `exposedbydefault=false` inverts that: a container is invisible
to Traefik unless it explicitly opts in with `traefik.enable=true`. A
database, a worker process, a sidecar — none of them are accidentally
routable just because they happen to be running.

**`--providers.docker.network=harbor`** — when Traefik forwards a request to
a container, it has to know which Docker network to reach it on (a container
can be on several). This pins it to the `harbor` network specifically, so a
container that's *also* on some other network (like `shared-db`, which
My_River_level joins to reach the shared Postgres server) is still routed to
correctly over `harbor`.

**`--entrypoints.web.address=:80`** and the two redirection lines —
Traefik listens on port 80 purely to immediately 301-redirect every request
to the HTTPS entrypoint. Nothing is ever served in plaintext.

**`--entrypoints.websecure.address=:443`**, **`.tls=true`**,
**`.tls.certresolver=cloudflare`** — the real entrypoint. `tls=true` turns
on TLS termination for this entrypoint (Traefik decrypts the request, the
backend container never sees TLS at all — this is why containers just run
plain HTTP internally, like uvicorn's default). `certresolver=cloudflare`
says "when you need a certificate for a hostname on this entrypoint, use the
resolver named `cloudflare`" (the one configured a few lines down with the
DNS-01/Cloudflare settings).

**`--entrypoints.websecure.http.tls.domains[0].main=...` / `.sans=...`** —
this is what makes the certificate a *wildcard* certificate rather than one
issued on-demand per hostname. Without this, Traefik would request a fresh
certificate the first time each new hostname was actually requested (the
"default" TLS behavior), which is slower on first hit and makes many more
calls to Let's Encrypt over time. Declaring the domain and its wildcard SAN
up front makes Traefik fetch **one** certificate at startup covering
`hpz440.ohr3023.org` and everything under `*.hpz440.ohr3023.org`, and every
new `<name>.hpz440.ohr3023.org` route added later is covered instantly, with
zero additional ACME calls.

**`--entrypoints.traefik.address=:8081`** and **`--api.insecure=true`** —
a third entrypoint, dedicated to Traefik's own read-only API and dashboard.
`api.insecure=true` sounds alarming but only means "serve the API without
requiring TLS or auth of its own" — the actual access control is entirely
in the `ports:` section below, which only publishes this entrypoint on
`127.0.0.1:8081` (loopback on the host) — nothing on the tailnet or the LAN
can reach it directly. It *is* reachable as `traefik:8081` from any other
container on the `harbor` network, which is a deliberate, accepted, narrow
exposure: the API is read-only (it can't change routes, only report them),
and every container on `harbor` is already one this host's operator chose to
run.

**`--certificatesresolvers.cloudflare.acme.*`** — configures the ACME
(automatic certificate management) client named `cloudflare` that the
`websecure` entrypoint references above. `email` is where Let's Encrypt sends
expiry warnings if renewal ever fails silently. `storage=/letsencrypt/acme.json`
is where the issued certificate and its private key are persisted — this
path is inside the `letsencrypt` named volume (declared at the bottom), so it
survives `docker compose down` / `up`. `dnschallenge=true` and
`dnschallenge.provider=cloudflare` select DNS-01 via Cloudflare specifically
(Traefik supports dozens of DNS providers; `cloudflare` here is a string
literal Traefik's `lego` library recognizes, not a hostname). `resolvers`
was explained above under "What actually happens on renewal."

**`ports:`** — the entire access-control story for this container lives in
these three lines, and they are the direct implementation of ADR 7 ("bind
the Tailscale address only, never `0.0.0.0`") extended by ADR 15 to the edge.
`${TAILNET_ADDRESS:?...}` is a Compose interpolation with a *mandatory*
guard: if the environment variable is unset or empty, Compose refuses to
start the container at all, with the error message inside the `:?`. Without
this guard, an unset variable silently becomes an empty string, and
`":80:80"` — empty host address — means **bind all interfaces**, i.e.
`0.0.0.0:80`, publishing the reverse proxy to the entire LAN. The guard turns
a silent security hole into a loud startup failure. `TAILNET_ADDRESS` itself
is written into `deploy/traefik/.env` by `install.sh` at deploy time (see
"How install.sh assembles this" below); it is never hand-typed.

**`env_file: /etc/traefik/env`** — this is a *different* mechanism from
`${TAILNET_ADDRESS}` above and it's important not to confuse them:
- `${VAR}` interpolation (used in `ports:` and the `command:` ACME email
  line) is resolved by the `docker compose` CLI itself, by reading a file
  named `.env` sitting **beside** the compose file (`deploy/traefik/.env`).
  It happens before the container is even created, and the substituted
  values become literal strings baked into the container's config.
- `env_file:` tells Docker to load a file's contents as **environment
  variables inside the running container**, at container-start time. This is
  how `CF_DNS_API_TOKEN` reaches Traefik's ACME client (which reads it from
  its own process environment) without ever appearing in the compose file,
  a command-line argument, or `docker inspect` output as a build-time value.

Two different files, two different mechanisms, two different files could
even (in principle) supply the same variable name and they'd remain
independent — that's not a coincidence here, since `TAILNET_ADDRESS` and
`ACME_EMAIL` are consumed via `${...}` interpolation from
`deploy/traefik/.env` (a file `install.sh` generates, safe to be templated
into a URL/YAML), while `CF_DNS_API_TOKEN` is consumed via `env_file` from
`/etc/traefik/env` (a file only root can read, never templated anywhere,
never appears in a rendered config).

**`volumes:`** — `/var/run/docker.sock:...:ro` is how the Docker provider
watches containers; `:ro` means Traefik can *observe* containers but cannot
create, stop, or modify them through this mount (though note: anyone who can
exec into the Traefik container could still use the socket to control Docker
generally — this is the standard, accepted trade-off of any Docker-aware
reverse proxy, not something specific to our setup). `./dynamic:...:ro` is
the file provider's directory (see "The file provider and harbor-console"
below). `letsencrypt:/letsencrypt` persists the certificate.

**`labels: harbor.kind: edge`** — this is *our* convention
(`harbor-console`'s, not Traefik's), read by this repository's own
`directory.py` policy module, not by Traefik. It tells the harbor-console
status page "this container is the edge itself" — it is allowed to publish
host ports (80, 443, and loopback 8081) without being flagged as
`bypasses-proxy`, and it is not itself something that needs a `traefik.*`
label (it doesn't route through itself). See "Declaring a service" in
CLAUDE.md for the full label vocabulary.

### Declaring a container to Traefik

A container that wants to be reachable joins the `harbor` network and
carries three labels (a fourth is optional). Using ParkSmart as the running
example:

```yaml
services:
  parksmart:
    build: .
    labels:
      traefik.enable: "true"
      traefik.http.routers.parksmart.rule: Host(`parksmart.hpz440.ohr3023.org`)
      traefik.http.services.parksmart.loadbalancer.server.port: "8000"
    networks:
      - harbor
      - default

networks:
  harbor:
    external: true
```

- **`traefik.enable: "true"`** — opts this specific container into routing,
  overriding `exposedbydefault=false` for this container alone.
- **`traefik.http.routers.parksmart.rule=Host(...)`** — defines a *router*
  named `parksmart` (the name is arbitrary but must be unique per host — it's
  what shows up in `docker --context hpz440 exec traefik wget -qO- http://localhost:8081/api/http/routers`
  and in the harbor-console page's rows) and its matching rule: "match any
  request whose `Host` header is exactly `parksmart.hpz440.ohr3023.org`."
  Traefik supports much richer rules (`PathPrefix`, `Header`, boolean
  combinations with `&&`/`||`) but a plain `Host()` match is all any service
  on this host needs, since each gets its own hostname.
- **`traefik.http.services.parksmart.loadbalancer.server.port=8000`** —
  defines a *service* (the backend Traefik actually forwards to) named
  `parksmart`, pointing at port 8000 of the container. Traefik's Docker
  provider auto-detects the container's port when the image `EXPOSE`s
  exactly one port, but it's declared explicitly here (and everywhere in
  this fleet) because relying on auto-detection is exactly the kind of
  implicit behavior that breaks silently when someone adds a second exposed
  port later. Router and service names don't have to match, but keeping
  them identical (both `parksmart`) is the convention across this fleet and
  makes the labels self-documenting.
- **`networks: [harbor, default]`** — the container must be attached to
  `harbor` or Traefik's Docker provider (which only watches that network,
  per `--providers.docker.network=harbor`) cannot reach it, no matter how
  correct its labels are. `default` (or whatever network the app needs for
  its own dependencies, like `shared-db` for My_River_level) stays alongside
  it — a container can be on multiple networks simultaneously.
- No **`ports:`** entry at all. The container's port 8000 is reachable
  *only* from other containers on `harbor` (Traefik, specifically) — never
  from the host, never from the tailnet directly.

For a service that isn't HTTP (MQTT, a raw TCP protocol Traefik isn't
fronting), the convention is different — `harbor.kind: tcp` plus
`harbor.port: <n>`, and the container *does* keep a published port, because
there is no HTTP-aware proxy step for it to hide behind. See CLAUDE.md's
label table for the complete set of kinds (`http`, `tcp`, `internal`,
`edge`).

### The file provider and harbor-console

The status page itself, `harbor-console-web`, is **not** a Docker container
— it's a plain Python process run by systemd directly on the host (see
[deployment.md](deployment.md)), because it needs to read the Docker socket
and the host's own metrics, and running the thing that monitors Docker
*inside* Docker adds a layer of indirection for no benefit. That means it
has no labels for the Docker provider to discover.

Traefik's **file provider** covers this: it watches
`deploy/traefik/dynamic/` (mounted read-only into the container at
`/etc/traefik/dynamic`) for YAML files defining routers and services exactly
the way the Docker provider would infer them from labels, just written out
directly:

```yaml
# deploy/traefik/dynamic/harbor.yml (rendered from harbor.yml.in by install.sh)
http:
  routers:
    harbor:
      rule: Host(`harbor.hpz440.ohr3023.org`)
      entryPoints:
        - websecure
      service: harbor
  services:
    harbor:
      loadBalancer:
        servers:
          - url: http://100.69.239.123:8100
```

The source file checked into git is `harbor.yml.in`, with the literal
placeholder `@TAILNET_ADDRESS@` instead of a real IP (a repo shouldn't hard-code
a specific host's address). `install.sh` renders it to `harbor.yml` at deploy
time with `sed`, writing to a temp file and `mv -f`ing it into place — not
writing the target file directly — because `--providers.file.watch=true`
means Traefik is actively watching that file, and a partial write from a
truncating redirect could be read by Traefik mid-write as invalid or
incomplete YAML. `harbor.yml` itself (the rendered file, containing the real
IP) is gitignored, alongside `.env`.

Note the URL: `http://100.69.239.123:8100`, not `http://harbor-console-web:8100`
or similar — because this backend isn't a container on the `harbor` network,
Traefik can't resolve it by container name at all. It reaches it exactly the
way any other client would: over the tailnet, at the address the process
itself bound. This is also why the routers list on the live host shows this
one as `harbor@file` (provider suffix `file`) while every container-based one
shows `<name>@docker` — the suffix tells you which provider is authoritative
for that particular route, and it's the first thing to check when a route
behaves unexpectedly.

### How `install.sh` assembles all of this

Summarizing the order of operations `deploy/install.sh` performs, since it's
scattered across the file:

1. **Prerequisite checks, before anything is touched**: `docker` and
   `tailscale` are on PATH; if the `harbor` Docker network already exists, it
   must carry the fixed bridge name `br-harbor` (see "The ufw problem"
   below) or the script refuses and explains how to recreate it; `/etc/traefik/env`
   exists; `ACME_EMAIL` is set within it (parsed with `grep`, never sourced
   — see "Secrets" below for why); `tailscale ip -4`
   returns a real address. Any failure here exits before `rsync`, before
   `uv sync`, before touching systemd — the host is left exactly as it was.
2. Sync the repository to `/opt/harbor-console`, build the venv, ensure the
   `harbor` service user exists.
3. **Restart both systemd units first** — this matters. Until
   `harbor-console-web` restarts on the code that binds port **8100**
   instead of port 80, it's still holding the tailnet address on port 80
   from the *previous* install. If Traefik tried to start before this step,
   its own attempt to bind `100.69.239.123:80` would fail with "address
   already in use" — this exact failure happened during the very first
   cutover and is why the ordering is what it is now (see git history for
   `fix(deploy): restart the units before the edge starts`).
4. **Prepare the edge**: `chmod 0600 /etc/traefik/env` (belt-and-braces,
   the file should already be 0600); create the `harbor` Docker network with
   the fixed bridge name if it doesn't exist yet; add the `ufw` rule if
   `ufw` is active (next section); write `deploy/traefik/.env` from
   `TAILNET_ADDRESS` and `ACME_EMAIL`; render `harbor.yml` from its
   template; `docker compose up -d --remove-orphans` in `deploy/traefik/`.
5. Bring up `deploy/hosted/compose.yaml` (Portainer, Watchtower — see below).
6. Print the new page URL.

### The ufw problem, and why there's a firewall rule in a deploy script

This is worth documenting in detail because it was **not** anticipated by
the original design and was only discovered when the very first cutover
attempt hung: Traefik's `harbor.yml` file-provider route pointed at
`http://100.69.239.123:8100`, and Traefik (inside a Docker bridge network
container) simply could not reach it — the connection timed out silently.

The cause: `ufw` (Uncomplicated Firewall) is active on hpz440, with a
default-deny `INPUT` policy. Docker's own iptables rules handle **container
↔ internet** and **container ↔ container** traffic (via the `FORWARD` and
`nat` chains) automatically, but they do nothing for **container → an IP
address that belongs to the host itself** — that traffic still has to pass
through the host's own `INPUT` chain, same as if it arrived from any other
machine, and `ufw`'s default policy there is DROP. A container calling out
to `100.69.239.123:8100` — the host's own tailnet address, from the host's
point of view — is exactly this case.

The fix, in `install.sh`:

```bash
docker network create -o com.docker.network.bridge.name=br-harbor harbor
...
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  ufw allow in on br-harbor to "${TAILNET_ADDRESS}" port 8100 proto tcp comment 'harbor-console: traefik -> page'
fi
```

Two things had to happen together:

1. The `harbor` Docker network needs a **predictable** bridge interface name
   for the `ufw` rule to reference (`ufw allow in on <interface>`). By
   default, Docker names bridge networks something like `br-a1b2c3d4e5f6`
   (a hash of the network's internal ID), which is not knowable in advance
   and would change if the network were ever recreated. Creating the network
   with `-o com.docker.network.bridge.name=br-harbor` pins the name.
2. The rule itself is scoped as tightly as the requirement allows: **on**
   the `br-harbor` interface specifically (so it says nothing about any other
   Docker network or any non-Docker traffic), **to** the tailnet address
   specifically (not `any`), **port 8100** specifically (not the whole
   host). This is the narrowest rule that solves the actual problem — every
   *other* container-to-host path on this host remains exactly as `ufw`
   denies it by default.

This is why `install.sh`'s prerequisite check refuses to proceed if the
`harbor` network already exists without the `br-harbor` name — a network
created before this fix (or by hand) would silently make the `ufw` rule a
no-op, since it would be naming an interface that doesn't exist, and the
page would go unreachable through the edge with no obvious error message at
all. The check makes that failure loud and immediate instead.

**If you ever see the harbor-console page unreachable through
`https://harbor.hpz440.ohr3023.org/` but reachable directly at
`http://100.69.239.123:8100/`, this firewall rule is the first thing to
check** — see Troubleshooting.

### Portainer and Watchtower

Two pieces of host infrastructure that predate this whole setup — Portainer
(a Docker management UI) and Watchtower (automatic image updates) — were
originally started by hand with `docker run`, not from any compose file, so
they carried no labels and weren't declared anywhere. `deploy/hosted/compose.yaml`
recreates them as proper labelled, composed services:

```yaml
services:
  portainer:
    image: portainer/portainer-ce:latest
    container_name: portainer
    restart: always
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - portainer_data:/data
    networks:
      - harbor
    labels:
      traefik.enable: "true"
      traefik.http.routers.portainer.rule: Host(`portainer.hpz440.ohr3023.org`)
      traefik.http.services.portainer.loadbalancer.server.port: "9000"

  watchtower:
    image: containrrr/watchtower
    container_name: watchtower
    restart: always
    environment:
      WATCHTOWER_LABEL_ENABLE: "true"
      WATCHTOWER_CLEANUP: "true"
      WATCHTOWER_SCHEDULE: "0 0 4 * * *"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    labels:
      harbor.kind: internal

volumes:
  portainer_data:
    external: true
networks:
  harbor:
    external: true
```

Two things worth calling out:

- **`portainer_data: external: true`** — this tells Compose "this volume
  already exists, don't create a new empty one, use the existing one." The
  original hand-run container had already been writing its configuration,
  users, and endpoint list into a Docker volume named `portainer_data`. Moving
  Portainer under Compose management only required removing the old
  container (`docker rm -f portainer` — the volume is independent of the
  container and is untouched by that) and starting the new one with the same
  volume name; every setting Portainer had was still there on first login
  after the migration.
- **Portainer's own port is 9000**, not 9443 (which was the *old*, TLS-terminating,
  loopback-only port it used to publish by hand). Since Traefik now
  terminates TLS for every service uniformly, Portainer's *own* built-in TLS
  listener is no longer needed — the label points at its plain-HTTP internal
  port instead, and Traefik supplies the `https://portainer.hpz440.ohr3023.org/`
  certificate exactly like every other service.
- Watchtower is `harbor.kind: internal` — it does nothing HTTP, has no
  interface of its own, and simply needs to exist on the host's Docker
  socket to do its job. It shows up on the harbor-console page as an
  `INTERNAL` row so it's visibly accounted for, without a route.

## Layer 3: Docker networking (the part that ties it together)

None of the above works without one Docker network that both Traefik and
every backend container share: `harbor`.

```bash
docker network create -o com.docker.network.bridge.name=br-harbor harbor
```

This is a standard Docker **bridge** network — an isolated virtual switch
that Docker creates on the host, with its own private IP subnet (Docker
picks one automatically, e.g. `172.25.0.0/16`), to which containers attach.
Two containers on the same bridge network can reach each other by **container
name** acting as a DNS hostname (Docker runs an embedded DNS resolver for
each bridge network) — this is how Traefik reaches `parksmart-parksmart-1`
just by the router config pointing at container port 8000, with no IP
address involved anywhere.

It is declared `external: true` in every project's compose file
(`networks: { harbor: { external: true } }`) rather than letting each
project define and create its own copy, because a network is only useful for
this purpose if every project that joins it is joining the *same* one.
`external: true` tells Compose "don't create this, it already exists,
connect to it" — the network itself is created exactly once, by
`install.sh`, and lives independently of any single project's lifecycle
(removing a project's containers never removes the network other projects
still depend on).

Nothing about this network is specific to Traefik conceptually — it's just
the one network every "wants to be routed" container has in common. Traefik
is simply the one container on it configured to watch the others.

## The full request, start to finish

Putting all three layers together, here is exactly what happens when someone
on the tailnet visits `https://parksmart.hpz440.ohr3023.org/`:

1. **DNS resolution**: the client's resolver asks Cloudflare's nameservers
   for `parksmart.hpz440.ohr3023.org`. The wildcard `*.hpz440` record
   answers `100.69.239.123`. (Cloudflare is not otherwise involved from here
   on — this was the only step it participated in.)
2. **Routing**: the client's OS looks up `100.69.239.123` in its routing
   table. Because the client is on the tailnet, Tailscale has installed a
   route for `100.64.0.0/10` pointing into the WireGuard tunnel, so the
   packet goes out over the tailnet mesh directly to hpz440, encrypted
   end-to-end by Tailscale's own transport (this is a second, independent
   layer of encryption underneath the TLS in the next step).
3. **TLS handshake**: the connection arrives at hpz440's kernel, addressed
   to `100.69.239.123:443` — the socket Traefik has bound. Traefik performs
   the TLS handshake using the wildcard certificate it obtained via DNS-01
   (Layer 1), and reads the decrypted HTTP request, including its `Host`
   header: `parksmart.hpz440.ohr3023.org`.
4. **Routing decision**: Traefik checks its live routing table — built by
   watching the Docker socket (Docker provider) and `dynamic/harbor.yml`
   (file provider) continuously, not read once at startup — for a router
   whose rule matches. `Host(`parksmart.hpz440.ohr3023.org`)` matches the
   router named `parksmart`, which points at the service named `parksmart`,
   which points at container `parksmart-parksmart-1` port 8000.
5. **Forwarding**: Traefik opens a plain (no TLS — it already terminated
   that) HTTP connection to `parksmart-parksmart-1:8000` over the `harbor`
   bridge network, using Docker's embedded DNS to resolve the container name
   to its private IP on that network, and forwards the request.
6. **Response**: the container's response travels back the same path in
   reverse — over `harbor` to Traefik, re-encrypted under TLS by Traefik,
   over the tailnet (and Tailscale's own encryption) back to the client.

At no point does the request touch a public IP address, a port published on
the host's own network stack (other than Traefik's own 80/443), or leave the
tailnet.

## Secrets: what's in git and what isn't

| File | Contains | In git? |
|---|---|---|
| `deploy/traefik/compose.yaml` | Traefik's config, referencing `${VAR}` placeholders | Yes |
| `deploy/traefik/dynamic/harbor.yml.in` | The page's route, with `@TAILNET_ADDRESS@` placeholder | Yes |
| `/etc/traefik/env` | `CF_DNS_API_TOKEN`, `ACME_EMAIL` — real secrets | **No.** Lives only on the host, root-owned, mode 0600. Created once by hand per the deployment doc; never written by any script that runs from the repo. |
| `deploy/traefik/.env` | `TAILNET_ADDRESS`, `ACME_EMAIL` — not secret, but host-specific | **No** (gitignored). Generated fresh by `install.sh` on every deploy. |
| `deploy/traefik/dynamic/harbor.yml` | The rendered route, with the real IP | **No** (gitignored). Generated fresh by `install.sh` on every deploy. |
| `letsencrypt` (Docker volume) | The issued certificate and private key | Not a file in the repo at all — a Docker-managed volume, persisted only on the host. |

The rule of thumb: anything that's the same on every host running this
project stays in git as a template. Anything that's specific to *this*
host (an IP address) or secret (an API token, a private key) is generated or
placed on the host directly and kept out of version control entirely.

**`/etc/traefik/env` is read, never sourced.** Early versions of
`install.sh` extracted `ACME_EMAIL` with `. /etc/traefik/env` (bash's
`source`) inside the root-run installer. That runs the file's contents as
shell code, not just reads it — a config file that is supposed to hold two
`KEY=value` lines had, in that version, the same power as a script, as
root, simply by existing on disk in that shape. `install.sh` now extracts
the value with `grep -E '^ACME_EMAIL=' /etc/traefik/env | cut -d= -f2-`,
which can only ever read text out of the file, never execute any of it.
Compose's own `env_file:` mechanism (used to hand `CF_DNS_API_TOKEN` to the
Traefik container) was never affected by this — it parses `KEY=value` pairs
directly and has no equivalent execution step.

## Troubleshooting

**The page is unreachable at `https://harbor.hpz440.ohr3023.org/` but works
at `http://100.69.239.123:8100/` directly.**
Almost certainly the `ufw` rule (see "The ufw problem" above) — either it
was never applied, or the `harbor` network doesn't have the `br-harbor`
bridge name the rule references. Check:
```bash
ssh gte@hpz440 'sudo ufw status | grep 8100'
ssh gte@hpz440 'docker network inspect harbor -f "{{index .Options \"com.docker.network.bridge.name\"}}"'
```
Expect the first to show the `br-harbor` rule and the second to print
`br-harbor`. If the second prints anything else (or an empty string), the
network needs to be recreated per the error message `install.sh` prints in
this situation.

**A container's route shows `ROUTE ERROR` on the harbor-console page.**
Two distinct causes, and the finding text on the page (or the router's
`error` field via the API) tells you which:
- *"Traefik does not report; is it on the harbor network?"* — the
  container has the right labels but isn't attached to the `harbor` network
  at all, so the Docker provider (scoped to `--providers.docker.network=harbor`)
  never sees it as eligible. Check `docker inspect <container> -f '{{json .NetworkSettings.Networks}}'`.
- *Traefik reports the router as `disabled`, with its own error text* — a
  labeling mistake it caught, most commonly a `loadbalancer.server.port`
  pointing at a port the container doesn't actually listen on, or a
  malformed `Host()` rule. Read Traefik's own error:
  ```bash
  ssh gte@hpz440 'curl -s http://127.0.0.1:8081/api/http/routers | python3 -m json.tool'
  ```

**The certificate never got issued / Traefik logs show ACME errors.**
Check the Cloudflare token is actually valid and scoped correctly *before*
suspecting anything else, since a bad token fails silently in ways that look
like a Traefik or DNS problem:
```bash
ssh gte@hpz440 'sudo sh -c "TOKEN=\$(grep -E \"^CF_DNS_API_TOKEN=\" /etc/traefik/env | cut -d= -f2-); curl -s -H \"Authorization: Bearer \$TOKEN\" https://api.cloudflare.com/client/v4/user/tokens/verify"'
```
(`grep` here, not `. /etc/traefik/env` — the file is root-owned config, not
trusted code; see "Secrets" below.)
Expect `"status":"active"`. Then watch the actual issuance attempt:
```bash
ssh gte@hpz440 'docker logs traefik --since 5m | grep -iE "acme|certificate|error"'
```
If you've hit Let's Encrypt's rate limit from repeated failed attempts
(roughly 5/hour/hostname on the production endpoint), the log will say so
explicitly, and the only fix is to wait out the window — there is no bypass.

**DNS doesn't resolve at all.**
```bash
nslookup harbor.hpz440.ohr3023.org 1.1.1.1
```
Confirm the wildcard record exists in Cloudflare and is **DNS only** (grey
cloud, not orange). If it resolves to something other than
`100.69.239.123`, someone edited the record or created a conflicting
non-wildcard one for that specific name (a specific `A` record for one
hostname always wins over the wildcard).

**A container that should be routed shows up as `undeclared-container` or
`bypasses-proxy` on the harbor-console page.**
This is the page working correctly, not a bug — it means the container is
running but doesn't carry the labels this document describes (or still
publishes a host port that no label accounts for). See "Declaring a
service" in CLAUDE.md and the label table above.

## Adding a new host later

Everything above assumes one host, `hpz440`, with one wildcard cert under
`hpz440.ohr3023.org`. A second host would need its own subdomain (e.g.
`*.host2.ohr3023.org`), its own wildcard DNS record pointing at *its own*
Tailscale address, and its own Traefik instance with its own ACME
certificate — Traefik instances don't coordinate or share certificates
across hosts in this design. Multi-host operation was explicitly deferred as
out of scope when this was built (see the spec's "Out of scope" section);
this note exists so a future reader doesn't assume it already works.
