# 16. Close the `harbor` network's attack surface: gate Traefik's API, isolate Portainer, scope the ufw rule by address

Date: 2026-09-19

## Status

Accepted

## Context

A security review of the reverse-proxy edge (ADR 15), run the day it went
live, found that moving every HTTP service onto one flat Docker network
(`harbor`) had widened what a single compromised container on that network
could reach, in three concrete ways:

- **Traefik's own API** (`--api.insecure=true`, entrypoint `:8081`) was
  served with no authentication at all. The host-side publish is loopback
  only, but that only restricts how the *host* reaches it; the same
  entrypoint answers as `traefik:8081` to anything on `harbor`, handing over
  the whole edge configuration -- every routed hostname, every backend
  container and port, every middleware -- for free.
- **Portainer** held a read-write Docker socket -- equivalent to root on the
  host -- while sitting on `harbor`, the same network every routed project
  container now joins. Any of those containers could reach `portainer:9000`
  directly, bypassing Traefik entirely.
- **The ufw rule** that lets Traefik reach the status page
  (`ufw allow in on br-harbor to ${TAILNET_ADDRESS} port 8100`) is scoped to
  an interface, not a source address. Every container on `harbor` shares
  that interface, so the rule admitted all of them to the page's
  unauthenticated inventory, not only Traefik.

A fourth finding -- container labels can steer outbound HTTP from the
`harbor-console-web` process (SSRF via a forged `Host()` rule) -- and a
fifth -- nothing detects two routers claiming the same hostname -- were
raised by the same review but not carried into this ADR: both require an
attacker already able to start labelled containers on this Docker daemon,
which is a privileged position the three fixes above do not assume, and
closing them well needs either a bigger change (host validation on the
probe path, or a `duplicate-host` finding) than fits alongside these three,
or conflicts with this project's no-auth-on-the-page stance (ADR 7). They
are left as known, accepted residual risk rather than folded in here.

## Decision

- **Traefik's `/api` is gated by HTTP Basic Auth**, via a file-provider
  router (`deploy/traefik/dynamic/api.yml`) rather than
  `--api.insecure=true`. The credential is generated once by `install.sh`
  into `/etc/traefik/env` and derived into two files on every run: an
  htpasswd form Traefik reads (root-only, mounted into the container), and
  a plaintext form `harbor-console-web` reads (`/etc/traefik/dashboard-password`,
  owned `root:harbor`, group-readable) to present the same credential --
  `harbor_console.webapp.read_traefik_credentials` and
  `harbor_console.traefik.traefik_routers`'s new `credentials` parameter. A
  missing or wrong credential degrades exactly like an unreachable API
  always has: `TRAEFIK_UNAVAILABLE`, a banner, no crash.
- **Portainer moves to its own Docker network, `admin`**, created by
  `install.sh` alongside `harbor`. No project container is on `admin`, so
  none can reach `portainer:9000` at all. Traefik joins both networks --
  it is the trusted routing component, not an untrusted workload -- and
  Portainer's `traefik.docker.network=admin` label tells Traefik which of
  its networks to route that one backend over. Portainer also gains the
  `traefik.http.routers.portainer.entrypoints=websecure` label it was
  missing, so its router no longer inherits the plaintext `:8081`
  entrypoint by Traefik's default of "every configured entrypoint."
- **The ufw rule is scoped to Traefik's own address on `harbor`**, not the
  bridge interface: `ufw allow in on br-harbor from <traefik-ip> to
  ${TAILNET_ADDRESS} port 8100`. `harbor`'s subnet was already fixed (Docker
  assigned `172.25.0.0/16` when the network was first created and nothing
  recreates it), so Traefik's address on it is stable without needing to
  pin an `ipv4_address` in compose. The rule is applied *after*
  `docker compose up` for Traefik, once that address is known, and is
  idempotent -- any earlier instance of the rule (by its comment tag) is
  removed first, so a stale allow for an address nothing uses any more is
  never left standing.

## Consequences

- A fresh `install.sh` run now prints a generated password once, the only
  time it is ever shown in full; it is retrievable afterward only by
  reading `/etc/traefik/env` as root. Rotating it is one line: remove
  `TRAEFIK_DASHBOARD_PASSWORD=...` from that file and re-run `install.sh`.
- `harbor-console-web` now needs one more file to read
  (`/etc/traefik/dashboard-password`); an operator restoring this host by
  hand from documentation rather than a full `install.sh` run has one more
  step to remember, consistent with `/etc/traefik/env` already being one.
- Traefik is now multi-homed (`harbor` and `admin`) rather than
  single-homed; this is the accepted cost of it being the one component
  trusted to bridge an otherwise-isolated admin plane, not a weakening of
  the isolation itself.
- The ufw rule now depends on `docker inspect traefik` succeeding after
  `docker compose up`; if it does not (Traefik failed to start), the
  script warns and leaves the page unreachable through the edge rather than
  falling back to the old, broader interface-scoped rule -- a fixable gap
  should surface as a fixable gap, not a silent reopening of the finding
  this ADR closes.
- SSRF via a forged container label and hostname-hijack via router priority
  remain open, deliberately, as noted in Context above.
