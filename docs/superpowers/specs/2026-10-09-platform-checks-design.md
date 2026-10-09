# Platform checks and the broken banner — design

Date: 2026-10-09
Status: approved in conversation, awaiting written review

## Why

On 2026-10-08 an `apt upgrade` moved Docker Engine from 28.3.3 to 29.8.2.
Docker 29 refuses API versions below 1.40; Traefik v3.3 pinned its Docker
provider to 1.24. Traefik kept running, the certificate stayed valid, the
file-provider route to the status page kept answering, and every
label-declared route answered 404 for 23 hours. Nothing restarted and nothing
failed loudly; the Traefik log repeated one line 38,000 times.

This is the third silent edge outage. ADR 17 (Traefik's default route landed
on the wrong network) and ADR 19 (a reboot moved Traefik off the address the
ufw rule named) both ended in gateway timeouts that nobody saw until they
looked. Each time the evidence was on the host, and in two of the three cases
it was already on the status page as `DOWN` rows. The gap is not evidence. It
is that nothing judged the evidence and nothing shouted.

This design adds the judgement and the shout: a small set of platform checks
computed from what the page already collects, written to a file both surfaces
read, and a banner that takes over the top of the status page and the tty1
console while any check fails.

## Scope

In scope:

- Checks on the host's own machinery: Docker, Traefik and its Docker provider,
  the page's own route through the proxy, the certificate, the edge's
  listeners, and the prober's own liveness.
- A red banner on both surfaces while any check fails.
- A `harbor-console-check` command, and `install.sh` ending with it so a
  deploy that leaves the platform broken fails in the terminal.

Out of scope, deliberately:

- A service-level tier. A single declared service being down stays a
  row-level `DOWN`. `arm` has been down for its own reasons for days; a banner
  that is red half the year stops meaning anything, and the next edge outage
  hides behind it.
- Notifications of any kind. Still in the founding document's deferred set.
- A pre-deploy rehearsal host. The question "would this upgrade break us" is
  answered by this loop within ninety seconds of it breaking us, which is the
  budget we can afford.
- Any new HTTP endpoint. The page stays the only thing the web service serves
  ([ADR 12](../../adr/0012-web-surface-collectors-and-conventions.md)).

## Decisions

- **The web prober judges; the console only reads.** The prober already
  collects everything the checks need. The console runs a 1 Hz loop that must
  never block, so it does no probing of its own; it reads one small file per
  tick.
- **The verdict crosses processes as a file**, `/run/harbor-console/checks.json`,
  the same way `snapshot.py` carries data between the prober and the renderer
  inside one process. The web unit gets `RuntimeDirectory=harbor-console`, so
  the directory exists, owned by `harbor`, at every start. `/run` is tmpfs; a
  reboot starts clean. The console unit runs as the same user and only reads.
  Rejected: a `/checks` endpoint pulled by the console (needs an ADR to retire
  the one-endpoint rule, a thread in the console, and ends up reading a cached
  result anyway), and a third timer unit (re-runs every collector, one more
  process to sandbox and keep alive).
- **Platform only.** The banner means "the host's machinery is broken", never
  "a project is broken". See Scope.
- **`unknown` never trips the banner.** A check whose evidence is missing says
  so and stays out of the verdict, the same rule the findings follow: absence
  of evidence is never a finding ([ADR 18](../../adr/0018-show-the-full-listening-inventory.md)).
- **Colour, for the banner only.** The founding document says no colours. A
  failure that has now happened three times is the bar the two earlier scope
  expansions met. ADR 21 narrows "no colours" to "no colours except the one
  that means the host is broken". Nothing else gets colour.
- **Thresholds are constants, not configuration.** Certificate warning at 14
  days (well inside Let's Encrypt's 30-day renewal window, so a silently
  failed renewal shows two weeks early). Staleness at three missed probe
  cycles (90 s), so one slow cycle never flashes the banner.

## Components

Keeps the collect / policy / render / coordinate split.

- **`certificate.py`** — collects. Opens a TLS connection to the tailnet
  address on 443 with SNI `harbor.hpz440.ohr3023.org`, 2 s timeout, stdlib
  `ssl` only. Returns `Certificate(subject_names, not_after)` or the sentinel
  `CERTIFICATE_UNAVAILABLE` with a reason. Never raises. The connector is
  injectable for tests.
- **`checks.py`** — policy, pure. `Check(name, state, reason)` with `state` in
  `ok`, `failed`, `unknown`. `run_checks(snapshot, now) -> tuple[Check, ...]`
  in a fixed order. `platform_broken(checks) -> bool` is true when any check
  is `failed`. `is_stale(written, now) -> bool` applies the 90 s threshold.
- **`verdict.py`** — contract, data only. `Verdict(written, hostname, checks)`.
  `dumps(verdict) -> str` and `loads(text) -> Verdict | None`; `loads`
  returns `None` for malformed input and never raises. `VERDICT_PATH` is a
  constant.
- **`snapshot.py`** — gains `own_route: Health | None` (the page probed at
  its own route through Traefik; `None` only when there is no tailnet
  address) and `checks: tuple[Check, ...]`.
- **`webapp.py`** — `collect_snapshot` probes the own route and the
  certificate alongside what it gathers today, then runs `run_checks` and
  stores the result in the snapshot. `probe_loop` takes a `publish_verdict`
  callable; the default writes the file atomically (temp name in the same
  directory, then `os.replace`). A write failure is printed to stderr once
  per distinct error and never stops the loop.
- **`web.py`** — renders the banner block when `platform_broken`, and a
  footer line otherwise.
- **`app.py`** — `run` gains `verdict_reader: Callable[[], Verdict | None]`,
  default reads and parses `VERDICT_PATH`. Passed to the renderer each tick.
- **`ui.py`** — `build_dashboard` gains `verdict: Verdict | None` and `now`,
  and renders the banner panel when broken or stale.
- **`check.py`** — the `harbor-console-check` entry point. Reads the file,
  prints, exits.

## The checks

| Name | Evidence | `failed` when | `unknown` when |
|---|---|---|---|
| `docker` | `docker_available` | Docker could not be read | never |
| `traefik-api` | `traefik_available` | the API could not be read, including 401 | never |
| `docker-provider` | routers and containers | at least one container declares `traefik.enable=true` and no router name ends in `@docker` | Docker or Traefik unavailable, or no container declares a route |
| `own-route` | `own_route` | the probe got 502, 503, 504 or no answer | no tailnet address |
| `certificate` | `certificate.py` | handshake failed, the subject names do not include `*.hpz440.ohr3023.org`, or fewer than 14 days remain | collector unavailable |
| `edge-listening` | listeners | no TCP listener on the tailnet address at 80 or at 443 | socket table unreadable |
| `prober-fresh` | the verdict's `written`, judged by the reader | older than 90 s | the file is missing |

`docker-provider` is the Docker 29 outage. `own-route` is the ADR 17 and
ADR 19 outages: the one route the page does not probe today is its own.
`certificate` is a wrong or revoked Cloudflare token showing up two weeks
before it matters. `prober-fresh` is judged by whoever reads, because a
writer cannot report its own silence: the page computes it from its
snapshot's `collected`, the console from the file's `written`. A missing
file on the console is shown as its own failure, "status page has not
reported", since a dead status page is platform breakage too.

`own-route` goes to `https://harbor.hpz440.ohr3023.org/` through Traefik
using the existing `probe`, so the same verdict rules apply as to any HTTP
row: any answer is up except the three the proxy invents. The probe is made
from the host, so it reaches Traefik on the tailnet address exactly as a
browser on the tailnet would.

## Rendering

**Web page.** When any check is `failed`, a full-width block above everything
else: red background, white bold text, first line `PLATFORM BROKEN`, then one
line per failed check as `<name>: <reason>`. When nothing has failed, a line
in the footer beside "Collected": `Platform checks: 7 passed`, with any
`unknown` checks named after it (`1 unknown: certificate`). The footer line is
what tells you the checks are running at all.

**Console.** When broken or stale, a 3-row `rich` Panel above the existing
table, red border, red bold text:

```
┌─ PLATFORM BROKEN ──────────────────────────────────────────────────────────┐
│ docker-provider: 8 containers declare routes, Traefik reports no @docker   │
│ own-route: https://harbor.hpz440.ohr3023.org/ answered 504                 │
└────────────────────────────────────────────────────────────────────────────┘
```

Two failures fit. A third and beyond collapse to `+N more, see the status
page`. Stale or missing shows the same panel with one line, `status page has
not reported since HH:MM`. Healthy shows no panel at all, so the 3 rows of
headroom from [ADR 20](../../adr/0020-size-the-console-for-the-room.md) stay
free, and the panel is pinned to 3 rows at 80 columns by a test the same way
the fullest GPU row is.

**`harbor-console-check`.** One line per check, `PASS <name>`, `FAIL <name>:
<reason>`, `UNKNOWN <name>: <reason>`, then a verdict line. Exit 0 when the
file is fresh and nothing failed, 1 when anything failed, 2 when the file is
stale or missing. No flags.

**`install.sh`.** After bringing up the edge and the hosted containers, poll
`harbor-console-check` every 5 s for up to 60 s. Print its last output. Exit
with its status if it is non-zero, after the existing closing lines, so a
deploy that leaves the platform broken ends in the terminal with the reason
instead of in the log a day later. The wait is needed because the prober's
first cycle after a restart takes up to 30 s and the own-route probe needs
Traefik to have reloaded its file route.

## Error handling

Collectors never raise, as today. A cycle that throws leaves the last snapshot
standing with `collection_error` and does **not** rewrite the verdict file, so
the file goes stale and the console reports that rather than a verdict from
evidence the cycle did not have. A verdict file that cannot be written (the
directory missing under an older unit, a read-only filesystem) is reported to
stderr once per distinct error and never takes the page down. A verdict file
that cannot be parsed is treated by every reader as missing.

## Testing

- `checks.py`: table-driven, one test per row of the checks table, for each of
  `ok`, `failed` and every `unknown` cause, with plain snapshots built in the
  test. `platform_broken` with mixed states. `is_stale` at the boundary.
- `verdict.py`: round trip; `loads` on empty, truncated, wrong-shape and
  non-JSON input returns `None`.
- `certificate.py`: injected connector returning a fake peer-cert dict with
  and without the wildcard SAN, with an expiry on either side of 14 days, and
  raising each of the `ssl` and socket errors.
- `webapp.py`: `collect_snapshot` with fake collectors produces `checks`;
  `probe_loop` with a fake `publish_verdict` writes once per successful cycle
  and not after a failed one; the default writer with a `tmp_path` writes
  atomically. No real files outside `tmp_path`, no real sockets.
- `app.py` and `ui.py`: `run` with a fake `verdict_reader`; the banner panel
  renders to exactly 3 rows at 80 columns for one, two and three failures and
  for stale; healthy renders no panel.
- `web.py`: the block appears only when a check failed; the footer line names
  unknowns.
- `check.py`: exit codes 0, 1 and 2 against fake file contents.
- `test_deploy.py`: the web unit carries `RuntimeDirectory=harbor-console`;
  `install.sh` invokes `harbor-console-check`.

## ADR

ADR 21, *Colour the platform banner, and nothing else*: records the three
outages as the bar, the "platform only" rule, the file contract, and the
narrowing of the founding document's no-colour stance to the banner.
