# harbor-console

A linux server attached monitor showing system health and projects status on every boot.

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
