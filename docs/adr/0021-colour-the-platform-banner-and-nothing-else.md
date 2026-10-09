# 21. Colour the platform banner, and nothing else

Date: 2026-10-09

## Status

Accepted

## Context

The founding document deferred colour: "No colors are required." Both
surfaces have been monochrome since, and the tty1 dashboard is read from
across a room, where a palette would be noise.

Three times now the edge has broken while every process stayed up and
nothing shouted. ADR 17: Traefik's default route landed on the wrong
network and every proxied request to the page timed out. ADR 19: a reboot
moved Traefik off the address the firewall rule named, same symptom,
different cause. On 2026-10-08 an `apt upgrade` to Docker Engine 29 refused
the API version Traefik v3.3 pinned; Traefik ran, the certificate stayed
valid, and every label-declared route answered 404 for 23 hours. In two of
the three the evidence was already on the status page as `DOWN` rows.
Nobody was looking, and the page did nothing to make them.

The earlier scope expansions (ADR 6, ADR 15) each had a failure that had
already happened as their bar. This one has three.

## Decision

We will judge the host's own machinery from what the page already collects
-- Docker, Traefik and its Docker provider, the page's own route through
the proxy, the served certificate, the edge's listeners, the prober's own
freshness -- and while any of those fails, both surfaces take over their
top with a red banner that says so: a block above everything on the status
page, three rows above the dashboard on tty1.

The banner is platform only. A declared service being down is a row-level
`DOWN`, as before. A check whose evidence is missing is `unknown` and never
trips the banner (ADR 18's rule, applied to judgement).

The verdict crosses from the web prober to the console as a file,
`/run/harbor-console/checks.json`, written atomically every cycle and read
once per 1 Hz tick. The console never probes. `harbor-console-check` reads
the same file so `install.sh` can end by failing loudly.

Red is the only colour either surface uses. It means one thing: the host
is broken. Nothing else -- not state cells, not findings, not GPU
temperatures -- gets a colour, so the banner cannot be confused with
decoration and its absence stays meaningful.

## Consequences

- An edge outage shows within 90 s on the monitor in the room and on the
  page, instead of in a log a day later.
- A deploy that leaves the platform broken exits non-zero in the terminal.
- A dev container down for its own reasons does not turn the host red. The
  cost is that a single broken service is still only a `DOWN` row; a
  second tier is explicitly deferred.
- The founding document's "no colours" narrows to "no colours but this one".
  Adding a second colour anywhere needs a new ADR.
- The page gains one probe of itself through the proxy and one TLS
  handshake per cycle, both with 2 s bounds, inside the existing prober
  thread. The console gains one small file read per tick.
- Rejected: a `/checks` endpoint pulled by the console (needs the
  one-endpoint rule retired and a thread in the console); a third timer
  unit (re-runs every collector); notifications (still deferred).
