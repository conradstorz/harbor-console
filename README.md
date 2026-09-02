# harbor-console

A linux server attached monitor showing system health and projects status on every boot.

## Status page

`harbor-console-web` serves a read-only page to the tailnet answering the other
question: what is running on this host, on which port, and is it up. It shows the
same health metrics as the attached monitor, the service directory from
`services.toml` with live or down state, and the ways the ledger and Docker
disagree — declared but not running, running but not declared, and running on a
port that does not match its lease.

It binds the host's Tailscale address and nothing else. If that address cannot be
determined it refuses to start rather than falling back to a broader one, because
the page is an inventory of every service on the host and binding *is* the access
control — which is why there is no login page
([ADR 7](docs/adr/0007-bind-tailscale-address-only.md)). Probing runs in a
background thread, so one hung service cannot make the page slow to load, and the
page is strictly read-only: nothing on it can start or stop anything.

It also serves `/ports.json`, which is how `harbor-console ports` learns what is
actually listening — including loopback-bound and non-Docker sockets that Docker
cannot report. Until its first probe cycle completes, that endpoint returns 503
rather than an empty list, so the allocator refuses to grant instead of trusting
state nobody has looked at yet.

Health probing is deliberately dumb: any HTTP response means up, including a
redirect to a login page. A project can offer `/hcstatus` returning a little JSON
to add detail to its row; a missing or broken one never makes it show as down.

Run it with `uv run harbor-console-web`. `deploy/install.sh` installs it as a
second systemd unit alongside the tty1 dashboard; the two have independent
lifetimes, so restarting one does not disturb the other.

## Port assignment

`harbor-console ports` hands out the published host ports for every project in
the tree, so two of them cannot claim the same one. A project opts in by adding a
`.harbor.toml` saying what it wants; harbor-console records a lease in
`services.toml`, writes the number it got into that project's own `.env` behind a
managed fence, and drops a `HARBOR_PORTS.md` next to it explaining the rules. The
incumbent always keeps its port — nothing running is renumbered — and new ports
come from 8100–8999.

| Command | Does |
| --- | --- |
| `uv run harbor-console ports scan` | Reports what would change. Writes nothing. |
| `uv run harbor-console ports sync` | Applies it: leases, `.env`, `.harbor.toml`, `HARBOR_PORTS.md`. Also repairs a project already holding its lease whose `.env` fence or `HARBOR_PORTS.md` has drifted — a fresh clone has no `.env`, and without the repair its compose file falls back to a default that may collide. |
| `uv run harbor-console ports sync --new-only` | Grants new requests only, withholding anything that would move a port a project already holds. This is what a scheduled run should use. |
| `uv run harbor-console ports show` | Prints the current lease table. Reads the ledger alone, so a broken `.harbor.toml` in some other project does not stop it. |

`scan` and `sync` exit non-zero when something is pending or wrong, so they are
usable from a script. A project moved off the port it asked for keeps reporting
drift — and keeps exiting non-zero — until its compose default is updated to the
number it was assigned; harbor-console does not edit compose files.

Ports are leased per `(host, addr, port)`: `0.0.0.0` claims a number across a
whole host, while two different bind addresses on one host can each hold the
same number. See [ADR 8](docs/adr/0008-allocate-ports-rather-than-validate.md)
for why the registry allocates rather than merely validates, and
[ADR 10](docs/adr/0010-address-scoped-port-key.md) for the key.
