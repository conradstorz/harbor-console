# Automatic deployment from `main`

Date: 2026-10-21

## Goal

Keep hpz440 running what `main` says. Today a deploy is two manual steps by
one person (`git pull` in `/srv/harbor-console`, then `sudo bash
deploy/install.sh`), and the checkout has lagged `main` for days at a time;
on 2026-10-21 the watchtower fix was merged and the host's checkout still
pointed at the previous merge. The project's rule is "main is always
deployable"; this makes it "main is deployed".

## Approach

A root `systemd` timer on the host polls GitHub and runs the installer when
`main` has moved. Pull-based: no inbound credential, no GitHub Actions
runner, no SSH key with root on the host, nothing new listening.

Rejected: a GitHub Actions workflow deploying over Tailscale SSH (a deploy
key that is root on the host, held by GitHub). Rejected: containerising
harbor-console so Watchtower updates it (the console owns tty1 and the web
surface reads host sysfs, `/proc` and the Docker socket by design). Rejected:
a webhook listener (a new inbound surface on a host whose whole exposure is
`:80` and `:443` on the tailnet, ADR 15).

## Components

All in `deploy/`.

**`update.sh`**, run as root by the service. Steps, each failing loudly:

1. Take an exclusive `flock` on `/run/lock/harbor-console-update.lock`;
   exit 0 at once if another run holds it.
2. Resolve the checkout from `$HARBOR_CHECKOUT` (set by the unit drop-in,
   see below) and its owner from `stat -c %U`. Every `git` command runs as
   that owner via `runuser -u <owner> --`, so root never touches the
   checkout's objects and git's `safe.directory` check is never involved.
3. Refuse (exit 1, message to stderr) if the checkout is not on `main`, or
   has tracked modifications (`git status --porcelain --untracked-files=no`
   is non-empty). Untracked files are allowed; the host checkout has two.
4. `git fetch --quiet origin main`. If `HEAD` equals `origin/main`, exit 0
   with no output: this is the common case, every five minutes, and the
   journal should stay quiet.
5. Refuse if `HEAD` is not an ancestor of `origin/main` (local commits the
   remote does not have). The script never resets, never force-pulls, never
   discards work; a diverged checkout is a human's problem and the refusal
   says so.
6. Log `updating <old> -> <new>` and `git pull --ff-only`.
7. `bash deploy/install.sh` from the checkout. Its exit code, which is the
   acceptance check's, is the script's exit code.

**`harbor-console-update.service`**, `Type=oneshot`, `ExecStart=/bin/bash
/opt/harbor-console/deploy/update.sh`, journal output. Note the script runs
from the installed copy, so a broken `update.sh` on `main` cannot deploy
itself until a working one is installed by hand, and a working one replaces
itself on the next cycle through `install.sh`'s rsync.

**`harbor-console-update.timer`**, `OnBootSec=5min`,
`OnUnitActiveSec=5min`, `Persistent=false` (a missed tick is just the next
tick), `WantedBy=timers.target`.

**Drop-in written by `install.sh`** at
`/etc/systemd/system/harbor-console-update.service.d/checkout.conf`:

```
[Service]
Environment=HARBOR_CHECKOUT=<REPO_ROOT>
Environment=PATH=<dir of uv>:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
```

`REPO_ROOT` is the directory `install.sh` was run from, and the `uv`
directory is `dirname "$(command -v uv)"` at install time. The checkout path
is not hardcoded anywhere in the repository; whatever checkout you install
from is the one the timer follows. The drop-in is rewritten on every
install, so running the installer from a different checkout moves the timer
to it.

**`install.sh`** installs the two unit files alongside the existing ones but
enables only the timer (`systemctl enable --now harbor-console-update.timer`);
the service is started by the timer, never enabled. **`uninstall.sh`**
disables the timer, removes both units and the drop-in directory.

## What the operator sees

- `systemctl list-timers harbor-console-update.timer` for the next run,
  `journalctl -u harbor-console-update.service` for history. A quiet journal
  is a current checkout.
- A deploy that breaks the web service stales the verdict file within 90 s,
  and `prober-fresh` colours the banner. A deploy whose acceptance check
  fails leaves the unit in `failed` and the same banner on both surfaces,
  exactly as a manual `install.sh` run would.
- A `git` refusal (diverged or dirty checkout) leaves the unit `failed` and
  the old code running correctly. Nothing on the page shows that updates
  have stopped flowing. That is the same class of silence as the watchtower
  loop, and a `deploy-fresh` platform check is the obvious follow-up; it is
  out of scope here because it needs a state file and a definition of "too
  old" that this spec should not guess at.

## Trust boundary

Root on hpz440 runs whatever lands on `main`. That was already true of every
manual deploy; the timer removes the human glance between merge and run. The
boundary is the GitHub repository's `main` branch and the account that can
push to it. The fetch is over HTTPS from a public repository and needs no
credential on the host.

## ADR 25

"Deploy `main` automatically from a host timer." Records the pull-based
choice, the three rejections, the trust boundary and the `deploy-fresh` gap.

## Tests (`tests/test_deploy.py`)

Shell is not unit-tested here; the guards pin what has to stay true:

- `update.sh` contains `--ff-only`, `flock`, `runuser`, `--is-ancestor`,
  `--untracked-files=no`, and contains neither `reset --hard` nor
  `--force`.
- The timer sets `OnUnitActiveSec=5min` and is `WantedBy=timers.target`;
  the service is `Type=oneshot`.
- `install.sh` enables the timer and writes `HARBOR_CHECKOUT=` into the
  drop-in; `uninstall.sh` names the timer and the drop-in directory.
- `install.sh`'s existing guard (`harbor-console-check` after the hosted
  compose) still holds.
