# 25. Deploy main automatically from a host timer

Date: 2026-10-21

## Status

Accepted

## Context

A deploy to hpz440 has been two manual steps by one person: `git pull` in
the checkout at `/srv/harbor-console`, then `sudo bash deploy/install.sh`.
The two drift apart (a re-run without the pull ships stale code silently),
and the checkout has lagged `main` for days at a time. On 2026-10-21 the
watchtower fix was merged and the host's checkout still pointed at the
previous merge. The project's rule is "main is always deployable"; in
practice main was deployed when someone remembered.

`install.sh` is already idempotent, already the only deploy path, and
already ends by failing loudly on the platform checks (ADR 21). What is
missing is something that runs it.

## Decision

We will run a root systemd timer on the host, `harbor-console-update.timer`,
every five minutes. It starts a oneshot service that runs
`deploy/update.sh` from the installed copy. The script fetches
`origin/main` as the checkout's owner, exits quietly when `origin/main` is
both checked out and recorded as installed, refuses when the checkout is not on `main`, has modified tracked
files, or has commits the remote lacks, and otherwise `git pull --ff-only`
and hands off to `install.sh`. It never resets, never force-pulls, never
discards work.

`install.sh` writes the checkout's full commit hash to
`/var/lib/harbor-console-update/deployed-commit` after every install step
and before its acceptance check, and `update.sh` compares `origin/main`
against that record, not against the checkout's `HEAD`, so a pull whose
install then failed is retried on the next tick instead of being counted as
deployed. The record is written before the acceptance check on purpose: the
check judges the platform, not the install, and a certificate problem must
not loop the installer. The fetch is wrapped in `timeout 120` and the pull
in `timeout 300`, because a stalled connection would otherwise hold the
lock (`/run/harbor-console-update.lock`) forever and the timer cannot start
a unit that is still activating. The unit itself has no timeout, so an
`install.sh` run is never killed partway.

`install.sh` installs both units, enables only the timer, and writes a
drop-in with the checkout path it was run from and the directory `uv` was
found in. Nothing in the repository hardcodes `/srv`; the timer follows
whatever checkout the installer was last run from.

## Consequences

- A merge to `main` is running on the host within about five minutes plus
  the installer's own time, and the acceptance check's exit code is the
  unit's result in journald.
- Root on hpz440 runs whatever lands on `main`. That was already true of
  every manual deploy; what goes is the human glance between merge and
  run. The trust boundary is the repository's `main` branch and the
  accounts that can push to it. It also includes whoever owns the checkout,
  since `install.sh` runs from that tree as root and `git status` cannot see
  every way an owner can alter it; the updater therefore refuses a checkout
  whose owner is neither root nor in the `sudo` group, so it never gives an
  account root it did not already have. The fetch is HTTPS from a public
  repository and needs no credential on the host.
- A refusal (diverged or dirty checkout) and a network outage both leave
  the old code running correctly and the unit `failed`, every five minutes
  until someone looks; the journal says which. A pull whose install failed
  is retried on the next tick, because the updater compares `origin/main`
  against the commit `install.sh` last finished installing
  (`/var/lib/harbor-console-update/deployed-commit`), not against the
  checkout's HEAD; a persistently failing install therefore re-runs, and
  restarts the units, every five minutes, which is loud on purpose. What
  nothing on the page shows is an updater that has stopped running at all:
  a `deploy-fresh` platform check is the obvious follow-up, and it needs a
  definition of "too old" this decision does not make.
- The record lives in a root-owned directory of its own, not in the web
  unit's `StateDirectory`: that one belongs to the unprivileged `harbor`
  user, and root writing a file into a directory another user owns is a
  symlink away from an arbitrary overwrite.
- This is the project's third persistent file, after the GPU and memory
  histories (ADR 22, ADR 23): one line, root-owned, under
  `/var/lib/harbor-console-update/`, removed by `uninstall.sh`.
- A broken `update.sh` on `main` cannot deploy itself: the service runs the
  installed copy, which only `install.sh` replaces. One manual run from the
  checkout repairs it.
- Rejected: a GitHub Actions workflow deploying over Tailscale SSH (a key
  that is root on the host, held by GitHub); containerising harbor-console
  so Watchtower updates it (the console owns tty1 and the web surface reads
  host sysfs, `/proc` and the Docker socket by design); a webhook listener
  (a new inbound surface on a host whose whole exposure is `:80` and `:443`
  on the tailnet, ADR 15).
