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
| `uv run harbor-console ports sync` | Applies it: leases, `.env`, `.harbor.toml`, `HARBOR_PORTS.md`. |
| `uv run harbor-console ports sync --new-only` | Grants new requests only, withholding anything that would move a port a project already holds. This is what a scheduled run should use. |
| `uv run harbor-console ports show` | Prints the current lease table. |

`scan` and `sync` exit non-zero when something is pending or wrong, so they are
usable from a script. See [ADR 8](docs/adr/0008-allocate-ports-rather-than-validate.md)
for why the registry allocates rather than merely validates.
