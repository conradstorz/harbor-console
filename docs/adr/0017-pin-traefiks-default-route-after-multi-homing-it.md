# 17. Pin Traefik's default route after multi-homing it

Date: 2026-09-19

## Status

Accepted

## Context

ADR 16 joined Traefik to a second Docker network, `admin`, so it could
reach Portainer's isolated Docker socket while remaining the one trusted
router between an otherwise-isolated admin plane and the rest of `harbor`.
That made Traefik multi-homed for the first time.

The status page, `harbor-console-web`, is not a container -- it binds the
host's tailnet address directly (ADR 7), an address on neither `harbor`'s
nor `admin`'s subnet. Reaching it from inside the Traefik container
therefore depends on which network the container treats as its default
route: that network's gateway is what NATs the request out to the host.
`install.sh` has always assumed the answer is `harbor` -- it reads
Traefik's `harbor`-network address (`docker inspect ... Networks.harbor`)
to build the ufw rule that admits it to port 8100, scoped to that one
address (ADR 16).

Multi-homing Traefik broke that assumption. Docker picked `admin` as its
default route instead, and did so reliably, not intermittently. Every
request through the edge to `https://harbor.hpz440.ohr3023.org/` then left
the container with a source address (`admin`'s subnet) the ufw rule never
allowed. ufw dropped it silently -- no rejection, no RST -- so the
connection simply hung until Traefik's own dial timeout expired, which it
answered to the client as a gateway timeout. The page itself was healthy
the whole time; `curl http://<tailnet-address>:8100/` from the host worked
throughout the outage.

Diagnosis (2026-09-19, on hpz440) traced this by layer: the rendered
router config (`dynamic/harbor.yml`) and the ufw rule
(`ufw status numbered`) both looked correct in isolation; `docker exec
traefik nc -zv <tailnet-address> 8100` reproduced the hang directly, ruling
out Traefik-internal causes; `/var/log/ufw.log` showed the actual blocked
packets arriving as `SRC=172.27.0.2` on `admin`'s bridge, not the
`172.25.0.2` `harbor` address the rule names -- confirming the source
address, not the rule, was wrong.

The first fix attempt used Compose's own `priority` key on the service's
per-network config, which is meant for exactly this. It failed: Compose
2.39.1 parses `priority` (confirmed via `docker compose config`, which
echoes it back) but does not apply it to the container when a service is
created attached to multiple networks at once -- the container came up
with `GwPriority: 0` on both networks regardless, and the default route
stayed on `admin`. This is a gap in Compose's implementation of the
feature, not a config mistake; the underlying Engine mechanism it should
be driving (`docker network connect --gw-priority`) works correctly when
called directly, confirmed live by disconnecting and reconnecting Traefik
to `harbor` with `--gw-priority 1000`, which fixed the route and made the
page answer `200` through the edge immediately.

## Decision

`install.sh` pins Traefik's default route explicitly, by calling the
Engine's own mechanism rather than trusting Compose to apply it:

- After `docker compose up` for the edge, read Traefik's current
  `harbor`-network `GwPriority` via `docker inspect`.
- If it is not already `1000`, run `docker network disconnect harbor
  traefik` followed by `docker network connect --gw-priority 1000 harbor
  traefik`. `--gw-priority` only takes effect on a fresh attach, not an
  update to an existing one, which is why the disconnect comes first.
- Skip both calls when the priority is already correct, so a routine
  re-run of `install.sh` -- idempotent everywhere else -- doesn't flap
  Traefik's `harbor` endpoint for no reason.

The compose-level `priority` key (`deploy/traefik/compose.yaml`) stays in
place alongside this, as the declared intent for whenever Compose actually
honors it; it is documentation now, not the enforcement.

## Consequences

- Reaching the status page through the edge no longer depends on which of
  Traefik's networks Docker happens to pick; `install.sh` makes it
  deterministic on every run.
- One more install-time dependency on `docker network connect` behaving as
  documented (Engine 28.3.3 / Compose 2.39.1 at time of writing). If a
  future Compose release fixes `priority`, this step becomes a no-op
  (`GwPriority` will already be `1000` from Compose itself) rather than
  something that needs removing.
- The failure mode this closes is silent by nature -- a dropped packet,
  not a rejected one -- so a future change that re-multi-homes Traefik (or
  moves the status page's collectors) should re-check this rather than
  assume the ufw rule alone is sufficient evidence the path works.
- Confirmed on hpz440 by deliberately breaking it (`docker network
  disconnect harbor traefik && docker network connect harbor traefik`,
  which drops back to `GwPriority: 0`) and re-running `install.sh`: it
  logged `Pinning Traefik's default route to the harbor network`, restored
  `GwPriority: 1000`, and the page answered `200` through the edge again.
