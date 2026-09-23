# 19. Reserve a fixed `harbor` address for Traefik, so the ufw rule survives a reboot

Date: 2026-09-23

## Status

Accepted

## Context

ADR 16 scoped the ufw rule that admits Traefik to the status page's port to
Traefik's own address on `harbor`, rather than to the `br-harbor` interface,
so that one compromised container on that network could no longer read the
page's unauthenticated inventory. It justified reading that address at
install time rather than pinning it:

> `harbor`'s subnet was already fixed (Docker assigned `172.25.0.0/16` when
> the network was first created and nothing recreates it), so Traefik's
> address on it is stable without needing to pin an `ipv4_address` in
> compose.

That premise is false, and a stable subnet is what makes it look true.
Docker hands out addresses from the subnet in the order containers attach;
a daemon or host restart re-runs that allocation from scratch. The subnet
never changes and the address inside it moves anyway -- no container has to
be recreated for it to happen.

hpz440 rebooted at about 16:37 UTC on 2026-09-23. Traefik's container, the
same one created on 2026-09-19, came back at 16:38:55 holding
`172.25.0.6` on `harbor`; ADR 17's own diagnosis records it at
`172.25.0.2` four days earlier. `172.25.0.2` now belongs to
`parksmart-parksmart-1`, which started earlier in the boot. The ufw rule,
written when `install.sh` last ran, still names `172.25.0.2`.

Every request through the edge to `https://harbor.hpz440.ohr3023.org/` then
left Traefik with a source address the rule does not allow, ufw dropped it
without a rejection, and the connection hung until Traefik's dial timeout,
which it answered as a gateway timeout -- symptom for symptom, the outage
ADR 17 closed, from a different cause.

Diagnosis (2026-09-23, on hpz440):

- The page itself was healthy throughout: `curl` to
  `http://100.69.239.123:8100/` from the host answered `200` in 1.7 ms,
  while the same page through Traefik answered `504` after 30.1 s.
- From inside the Traefik container, `wget` to `100.69.239.123:8100` timed
  out, and so did `wget` to `172.25.0.1:8100`, the `harbor` gateway --
  ruling out the default route, which ADR 17's pin had in fact survived the
  reboot at `GwPriority: 1000`.
- Opening that port from each of the eight containers on `harbor` in turn
  is what named the cause: `172.25.0.2` connected, and `.3`, `.4`, `.5`,
  `.6` (Traefik), `.7` and `.8` all timed out. A single-address allow for
  an address Traefik no longer holds behaves exactly like that.
- The rule itself was not read directly: neither `gte` nor `conrad` has
  passwordless sudo on this host, so `ufw status` and `/var/log/ufw.log`
  -- ADR 17's evidence -- were unavailable, and the behaviour above stands
  in for them.

ADR 17 already warned that this path fails silently by nature, "a dropped
packet, not a rejected one," and that a future change near it should
re-check rather than trust the rule as evidence the path works. A reboot
is not a change anyone makes, which is why this recurred unprompted.

## Decision

We will stop reading Traefik's `harbor` address and start reserving it.

- **`172.25.255.254` is Traefik's address on `harbor`.** It sits at the top
  of the `/16`, far above the addresses Docker hands out sequentially from
  `.2`, so dynamic allocation will not reach it. That is a convention, not
  an IPAM guarantee: nothing stops another container claiming it
  explicitly, and `--aux-address`, the one mechanism that would exclude it
  from the pool, can only be set when the network is created -- which
  `harbor` already was, with every project on it. The reservation is
  therefore defended by the two steps below rather than by Docker.
- **`deploy/traefik/compose.yaml` declares it** as `ipv4_address` under the
  service's `harbor` network. `harbor`'s IPAM already carries an explicit
  `172.25.0.0/16` subnet, so static assignment on it is available.
- **`install.sh` passes `--ip 172.25.255.254` on the reconnect** that ADR 17
  added, alongside `--gw-priority 1000`. That call creates the endpoint
  Traefik actually uses; without the flag it re-requests a dynamic address
  and undoes the compose declaration on every run.
- **The ufw rule keeps reading `docker inspect`** for the address it names,
  rather than hard-coding the constant in a second place. The rule should
  match the address Traefik really has; the point of this ADR is that the
  address stops moving, not that the rule stops looking.
- **The pin never trades an outage for an address.** `install.sh` checks who
  holds the reserved address before it detaches Traefik, and falls back to a
  dynamic attach with a warning when another container does. If the `--ip`
  attach fails anyway -- the same conflict arriving in the gap between the
  check and the attach -- it clears the endpoint and retries dynamically.
  Both paths are necessary because the disconnect cannot be cheaply undone:
  a `--ip` attach that loses the address leaves Traefik listed on `harbor`
  with an *empty* address rather than detached, which takes every route
  down and, further along, leaves the ufw step with no address to name and
  no rule written. A dynamic address is the failure this ADR set out to
  fix; no address is an outage.
- **`install.sh` warns when Traefik's actual `harbor` address is not the
  reserved one.** The rule it writes will be correct either way, so
  without the warning a failed pin leaves a working page and an invariant
  that has quietly gone back to drifting until the next reboot.

## Consequences

- A reboot no longer breaks the edge's path to the status page. This is the
  second outage from this one coupling and the first whose trigger was
  nobody's action, so closing it by construction is worth a reserved
  address.
- One constant now lives in two files, `compose.yaml` and `install.sh`,
  and they must agree. The warning above is what catches the disagreement.
- Traefik's `harbor` address becomes something to preserve rather than
  something to discover, which is a new constraint on anything that
  detaches Traefik from that network -- including ADR 17's own
  disconnect/reconnect, now the place the address is asserted.
- The failure stays invisible to the page. What breaks is reading the page
  through the edge, and nothing in the page reports "the edge cannot reach
  me" -- the direct URL looks perfect while the routed one times out. A
  finding for that is a real gap and deliberately not in this ADR; it needs
  the page to probe its own route, which is a wider change than a reserved
  address.
- The mechanism was checked on hpz440 before this went in, on throwaway
  containers rather than on the live edge (Engine 28.3.3): the reserved
  address is taken by `docker run --network harbor --ip 172.25.255.254`, and
  a `docker network disconnect` followed by `docker network connect --ip
  172.25.255.254 --gw-priority 1000` -- the exact call `install.sh` makes --
  brings it back with both the address and the priority intact. Both
  degraded paths were exercised the same way: with a second container
  holding the address, the preflight warns and attaches dynamically, and
  with the preflight suppressed to simulate the race, the attach fails with
  `Address already in use` and the retry recovers to a dynamic address with
  `GwPriority: 1000` -- never to the addressless endpoint that the failed
  attach leaves behind.
- Docker's own conflict behaviour is the sharp edge here and is worth
  restating: a failed `--ip` attach is not a no-op. It leaves the container
  attached to the network with no address at all, and a plain retry then
  fails with "endpoint already exists" unless the endpoint is disconnected
  first.
- Options rejected:
  - **Widening the rule back to `br-harbor` or to the subnet.** Fixes the
    reboot and reopens ADR 16's third finding: every container on `harbor`
    regains the page's inventory.
  - **A watcher that re-applies the rule when Traefik's address changes.**
    A daemon, a socket, and a race between the event and the rule, to
    maintain a value that did not need to vary.
  - **Containerizing the page so Traefik routes to it by service name.**
    The durable fix -- it deletes the host-address hop, the ufw rule and
    both of these outages at once -- but it reworks ADR 7's bind-is-the-
    access-control and the two surfaces' independent lifetimes. Worth
    revisiting if this coupling bites a third time; too large to bundle
    here.
