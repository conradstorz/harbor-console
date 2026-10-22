# 24. The repository's own containers are platform

Date: 2026-10-21

## Status

Accepted. Amends [ADR 21](0021-colour-the-platform-banner-and-nothing-else.md).

## Context

On 2026-10-21 the `watchtower` container was found restarting for the 637th
time. Docker Engine 29, installed on 2026-10-08, refuses the API version its
abandoned image pinned; it exited within a second of every start and
`restart: always` made that a loop. For a fortnight no image on the host was
updated and nothing on either surface said so.

Nothing could have. The container is declared `harbor.kind=internal`: no
route to probe, no port to check, by design. `docker.py` kept name, labels,
ports and networks from `docker inspect` and dropped `State`, so the one
piece of evidence that existed was fetched every cycle and thrown away. And
ADR 21 says a declared service being down is a `DOWN` row, never a check: a
banner red for one dev container stops meaning anything.

That rule was written for project containers. Traefik, Portainer and
Watchtower are not project containers. They come from this repository's own
compose files, `install.sh` brings them up, and the page's rows depend on
them: without Traefik there are no routes, without Watchtower no image on
the host is ever updated. A failure in one of them is the kind of failure
ADR 21 built the banner for.

## Decision

We will read each container's `State.Status`, `RestartCount`, `ExitCode` and
`State.Health.Status` from the inspect call the collector already makes, and
report a `restarting` container as a `crash-looping` finding and an
`unhealthy` one as an `unhealthy` finding, for any container Docker lists.
A declared container in either condition shows `RESTARTING` or `UNHEALTHY`
in place of whatever state its kind would have given it.

We will add a seventh platform check, `platform-containers`, that fails
when a container labelled `harbor.platform=true` is restarting or
unhealthy. The three containers this repository deploys carry the label in
their compose files, and `tests/test_deploy.py` guards that every service
there does. Nothing infers the label from compose project names or
container names; a container declares itself, as it does for everything
else here (ADR 15).

ADR 21's "a declared service being down is a `DOWN` row, never a check"
now reads "... never a check, unless the container declares itself
platform". Red still means one thing: the host's own machinery is broken.

## Consequences

- A crash loop in Traefik, Portainer or Watchtower reaches the monitor in
  the room and the page's banner within one prober cycle, and
  `harbor-console-check` exits non-zero on it, so a deploy that leaves one
  looping fails loudly.
- Every other container's loop or failed healthcheck is on the page as a
  finding and a red state cell, where it was invisible before. The banner
  stays theirs to earn: a project container adding `harbor.platform=true`
  is a one-line decision in its own compose file, and this repository
  keeps no list of who did.
- The restart count is detail text, never a trigger: it is cumulative
  since the container was created, and one restart a week ago would nag
  until the next recreate.
- A platform container that exits with no restart policy and stays down is
  absent from `docker ps`, and this check cannot miss it. All three carry a
  policy; Traefik, the `unless-stopped` one, is already covered by
  `traefik-api`, `docker-provider` and `edge-listening`. Reading
  `docker ps -a` to close the gap was rejected: a deliberately stopped
  container is not a finding, and the demonstrated failure is a loop.
- No new subprocess, no new dependency, no change to the verdict file's
  shape or to the console.
