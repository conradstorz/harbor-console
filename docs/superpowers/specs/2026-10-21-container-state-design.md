# Container state: crash loops and failed healthchecks

Date: 2026-10-21

## Goal

Surface a container that Docker itself knows is unwell. On 2026-10-21
`watchtower` was found restarting for the 637th time: Docker Engine 29 had
refused its client's API version since the 2026-10-08 upgrade, `restart:
always` turned every exit into another start, and nothing on either surface
said so. The container is declared `harbor.kind=internal`, so it has no
route and no port to probe, and `docker.py` kept name, labels, ports and
networks from `docker inspect` and dropped `State`. The only evidence of its
health was in the JSON the page already fetched, and the page threw it away.

Two signals, both read from that same `inspect` call, no new subprocess:

- **Crash loop**: `State.Status` is `restarting`.
- **Failed healthcheck**: `State.Health.Status` is `unhealthy`, for images
  that define one.

They appear as findings and row states on the page, and, for containers this
repository deploys itself, as a platform check that trips the red banner.

## Approach

Follow the existing split exactly. The collector gains fields; the policy
gains two findings and two row states; `checks.py` gains one check; the
renderer learns to colour two more states red. The verdict file carries the
check to the console unchanged.

Rejected: `docker ps -a` to see containers that exited and stayed down. A
deliberately stopped container is not a finding, and the demonstrated failure
is a loop, which `docker ps` already lists (a `restarting` container is in
the default listing; that is how watchtower appeared on the page as an
`INTERNAL` row while it was failing). Rejected: a finding on `RestartCount`
alone. The count is cumulative since the container was created, so one
restart a week ago would nag until the next recreate. It is detail text only.

## Collector: `docker.py`

`Container` gains four fields, all with defaults so every existing
constructor and test stays valid:

| Field | Source | Default |
|---|---|---|
| `status: str` | `State.Status` | `"running"` |
| `restarts: int` | `RestartCount` | `0` |
| `exit_code: int \| None` | `State.ExitCode` | `None` |
| `health: str \| None` | `State.Health.Status`; `None` when the image has no healthcheck | `None` |

`_parse` reads each with the same guards as the rest of the entry: a missing
or wrong-typed field takes its default, never raises.

## Policy: `directory.py`

Two findings, over every container, declared or not, withheld when Docker is
unavailable like the other container findings:

- `crash-looping`: `status == "restarting"`. Detail:
  `container 'watchtower' is restarting (637 restarts, last exit 1)`; when
  `exit_code` is `None` the parenthesis stops after the count.
- `unhealthy`: `health == "unhealthy"`. Detail:
  `container 'x' fails its own healthcheck`.

Two row states, `STATE_RESTARTING = "RESTARTING"` and `STATE_UNHEALTHY =
"UNHEALTHY"`. In `build_rows`, after the kind-specific row is built, a
container that is restarting gets `RESTARTING`, else one that is unhealthy
gets `UNHEALTHY`, replacing whatever state the kind produced. Restarting wins
because a looping container's healthcheck is meaningless. `starting` health is
not a finding and changes no row.

## Platform check: `checks.py`

A container declares itself as this repository's own infrastructure with the
label `harbor.platform: "true"`. Traefik (`deploy/traefik/compose.yaml`),
Portainer and Watchtower (`deploy/hosted/compose.yaml`) carry it. Nothing
infers it from compose project names or container names.

New check `platform-containers`:

- `unknown` when Docker is unavailable (the `docker` check already fails
  then; this one does not pile on).
- `failed` when any container carrying the label is restarting or
  unhealthy; the reason names the first such container, sorted by name, with
  the finding's detail text.
- `ok` otherwise, including when no container carries the label.

Limit, recorded in the ADR: a platform container that exited with no restart
policy is absent from `docker ps` and this check cannot see it. All three
carry `restart: always` or `unless-stopped`; Traefik, the `unless-stopped`
one, is already covered by `traefik-api`, `docker-provider` and
`edge-listening`.

`run_checks` takes the containers the prober already has and appends this
check to the tuple it returns, following its existing signature style.
`platform_broken` needs no change: a `failed` check trips it.

## Renderer: `web.py`

`RESTARTING` and `UNHEALTHY` render with the `down` class like `DOWN` and
`ROUTE ERROR`. The findings list is generic and needs no change.

## Console and verdict

Nothing changes. The check rides in `checks.json`; the banner and
`harbor-console-check` print whatever failed.

## ADR 24

"The repository's own containers are platform." Amends ADR 21's "a declared
service being down is a `DOWN` row, never a check" to exclude containers
labelled `harbor.platform`, with the watchtower loop as the failure that set
the bar, and the `docker ps -a` limit above.

## Tests

- `tests/test_docker.py`: `_parse` reads the four fields; defaults when
  `State` or `Health` is missing or malformed.
- `tests/test_directory.py`: each finding appears and is withheld without
  Docker; the row override for a restarting internal container, a restarting
  HTTP container that Traefik reports as live, and an unhealthy TCP
  container; restarting beats unhealthy; `starting` changes nothing.
- `tests/test_checks.py`: the three states of `platform-containers`; an
  unlabelled restarting container does not fail it.
- `tests/test_web.py`: the two states render red.
- `tests/test_deploy.py`: every service in the two compose files carries
  `harbor.platform: "true"`.
