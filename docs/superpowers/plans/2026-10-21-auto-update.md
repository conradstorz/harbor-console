# Automatic Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A root systemd timer on the host deploys `main` whenever it has moved, so the host's checkout stops lagging the repository.

**Architecture:** `deploy/update.sh` fetches `origin/main` as the checkout's owner, refuses anything but a clean fast-forward on `main`, pulls, and hands off to the existing `install.sh`. A oneshot service runs it from the installed copy; a timer fires it every five minutes; `install.sh` writes a unit drop-in carrying the checkout path and `uv`'s directory, so nothing in the repository hardcodes `/srv`. Shell is guarded by string tests in `tests/test_deploy.py`, the pattern this repository already uses for YAML and units.

**Tech Stack:** bash, systemd timer and oneshot service, pytest via `uv run pytest`.

Spec: `docs/superpowers/specs/2026-10-21-auto-update-design.md`.

## Global Constraints

- The script never resets, never force-pulls, never discards work. `git pull --ff-only` is the only write to the checkout.
- Every `git` command runs as the checkout's owner via `runuser -u <owner> --`, never as root.
- Quiet when current: `HEAD == origin/main` exits 0 with no output.
- Units: `harbor-console-update.service` (`Type=oneshot`) and `harbor-console-update.timer` (`OnBootSec=5min`, `OnUnitActiveSec=5min`, `Persistent=false`, `WantedBy=timers.target`). Only the timer is enabled.
- Drop-in path: `/etc/systemd/system/harbor-console-update.service.d/checkout.conf`, with `Environment=HARBOR_CHECKOUT=<REPO_ROOT>` and `Environment=PATH=<dir of uv>:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`.
- Lock file: `/run/lock/harbor-console-update.lock`, `flock -n`, exit 0 when held.
- Run `uv run pytest` before every commit; it must stay green.
- Commit messages end with the trailer line `Assisted-by: Claude Fable 5.1 (Claude Code)`. Do not add a `Co-Authored-By` line.
- Files in this repo are LF. Check `git ls-files --eol <file>` shows `w/lf` before committing; shell scripts with CRLF do not run.

---

### Task 1: `update.sh` and its two units

**Files:**
- Create: `deploy/update.sh`
- Create: `deploy/harbor-console-update.service`
- Create: `deploy/harbor-console-update.timer`
- Test: `tests/test_deploy.py`

**Interfaces:**
- Produces: the three files above. `update.sh` reads `HARBOR_CHECKOUT` from the environment and execs `bash "${HARBOR_CHECKOUT}/deploy/install.sh"`. Task 2 installs them and writes the drop-in.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deploy.py`:

```python
UPDATE_SCRIPT = REPO / "deploy" / "update.sh"
UPDATE_SERVICE = REPO / "deploy" / "harbor-console-update.service"
UPDATE_TIMER = REPO / "deploy" / "harbor-console-update.timer"


def test_the_updater_only_ever_fast_forwards_as_the_checkouts_owner():
    # ADR 25. The one write to the checkout is a fast-forward pull, run as
    # whoever owns the checkout so root never touches its objects. Anything
    # that could discard local work is a plan failure, not a flag.
    text = UPDATE_SCRIPT.read_text(encoding="utf-8")

    assert "--ff-only" in text
    assert "runuser -u" in text
    assert "merge-base --is-ancestor" in text
    assert "--untracked-files=no" in text
    assert "flock -n" in text
    assert "reset --hard" not in text
    assert "--force" not in text
    assert "checkout --" not in text
    assert "\r" not in text, "update.sh must be LF; CRLF shell does not run"


def test_the_updater_hands_off_to_the_installer_from_the_checkout():
    text = UPDATE_SCRIPT.read_text(encoding="utf-8")

    assert 'bash "${CHECKOUT}/deploy/install.sh"' in text
    assert "HARBOR_CHECKOUT" in text


def test_the_update_timer_fires_every_five_minutes_and_only_the_timer_is_installed():
    timer = UPDATE_TIMER.read_text(encoding="utf-8")
    service = UPDATE_SERVICE.read_text(encoding="utf-8")

    assert "OnBootSec=5min\n" in timer
    assert "OnUnitActiveSec=5min\n" in timer
    assert "Persistent=false\n" in timer
    assert "WantedBy=timers.target\n" in timer
    assert "Type=oneshot\n" in service
    assert "ExecStart=/bin/bash /opt/harbor-console/deploy/update.sh\n" in service
    assert "[Install]" not in service, "the timer starts the service; it is never enabled itself"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_deploy.py -q`
Expected: 3 failures, `FileNotFoundError`.

- [ ] **Step 3: Write the script and the units**

Create `deploy/update.sh` (LF line endings):

```bash
#!/usr/bin/env bash
# Harbor Console auto-update (ADR 25): deploy main when it has moved.
#
# Run as root by harbor-console-update.service, every five minutes from
# harbor-console-update.timer, from the installed copy under /opt. The
# checkout to follow comes from HARBOR_CHECKOUT, which install.sh writes
# into the unit's drop-in from wherever it was run; nothing here hardcodes
# a path. Quiet when there is nothing to do, loud when it refuses, and it
# never resets, force-pulls or discards work: a diverged or dirty checkout
# is a human's problem and this script says so and stops.
set -euo pipefail

LOCK=/run/lock/harbor-console-update.lock
CHECKOUT="${HARBOR_CHECKOUT:-}"

if [[ ${EUID} -ne 0 ]]; then
  echo "Error: update.sh must run as root (it hands off to install.sh)" >&2
  exit 1
fi
if [[ -z "${CHECKOUT}" ]]; then
  echo "Error: HARBOR_CHECKOUT is not set; re-run install.sh from the checkout to write the unit drop-in" >&2
  exit 1
fi
if [[ ! -d "${CHECKOUT}/.git" ]]; then
  echo "Error: ${CHECKOUT} is not a git checkout" >&2
  exit 1
fi

# One run at a time. install.sh restarts units and brings up compose
# projects; two of it interleaved is worse than one of it late. The lock is
# held on fd 9 through the exec below, so the installer runs under it too.
exec 9>"${LOCK}"
if ! flock -n 9; then
  exit 0
fi

owner=$(stat -c %U "${CHECKOUT}")
git_as_owner() {
  runuser -u "${owner}" -- git -C "${CHECKOUT}" "$@"
}

branch=$(git_as_owner rev-parse --abbrev-ref HEAD)
if [[ "${branch}" != "main" ]]; then
  echo "Error: ${CHECKOUT} is on '${branch}', not main; not deploying" >&2
  exit 1
fi
if [[ -n "$(git_as_owner status --porcelain --untracked-files=no)" ]]; then
  echo "Error: ${CHECKOUT} has local modifications to tracked files; not deploying" >&2
  exit 1
fi

git_as_owner fetch --quiet origin main
local_head=$(git_as_owner rev-parse HEAD)
remote_head=$(git_as_owner rev-parse origin/main)
if [[ "${local_head}" == "${remote_head}" ]]; then
  exit 0
fi
if ! git_as_owner merge-base --is-ancestor "${local_head}" "${remote_head}"; then
  echo "Error: ${CHECKOUT} has commits origin/main does not (HEAD ${local_head:0:9}, origin/main ${remote_head:0:9}); refusing to touch it" >&2
  exit 1
fi

echo "updating ${local_head:0:9} -> ${remote_head:0:9}"
git_as_owner pull --quiet --ff-only origin main

# install.sh is the deploy; its exit code is the acceptance check's.
exec bash "${CHECKOUT}/deploy/install.sh"
```

Create `deploy/harbor-console-update.service`:

```ini
[Unit]
Description=Harbor Console: deploy main when it has moved (ADR 25)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
# The installed copy, not the checkout: install.sh's rsync replaces this
# file with the checkout's on every deploy, so a fix to update.sh on main
# takes effect the run after the one that installs it.
ExecStart=/bin/bash /opt/harbor-console/deploy/update.sh
StandardOutput=journal
StandardError=journal
```

Create `deploy/harbor-console-update.timer`:

```ini
[Unit]
Description=Harbor Console: check main every five minutes (ADR 25)

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
# A missed tick is just the next tick; nothing to catch up on.
Persistent=false

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: Run the tests and a syntax check**

Run: `uv run pytest tests/test_deploy.py -q`
Expected: all pass.

Run: `bash -n deploy/update.sh`
Expected: no output (parses). On Windows this needs Git Bash, which the Bash tool is.

Run: `git ls-files --eol deploy/update.sh deploy/harbor-console-update.service deploy/harbor-console-update.timer` after `git add` in the next step
Expected: `w/lf` on every line.

- [ ] **Step 5: Commit**

```bash
git add deploy/update.sh deploy/harbor-console-update.service deploy/harbor-console-update.timer tests/test_deploy.py
git commit -F - <<'EOF'
feat: update.sh deploys main when it has moved, from a five-minute timer

Fetches as the checkout's owner, refuses anything but a clean fast-forward
on main, pulls, and hands off to install.sh under a lock. Never resets.

Assisted-by: Claude Fable 5.1 (Claude Code)
EOF
```

---

### Task 2: `install.sh` installs and enables the timer; `uninstall.sh` removes it

**Files:**
- Modify: `deploy/install.sh` (the unit install loop near line 146, the enable loop near line 224)
- Modify: `deploy/uninstall.sh` (the disable and remove loops near lines 35 and 40)
- Test: `tests/test_deploy.py`

**Interfaces:**
- Consumes: the three files from Task 1.
- Produces: the drop-in at `${UNIT_DIR}/harbor-console-update.service.d/checkout.conf`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deploy.py`:

```python
UNINSTALL = REPO / "deploy" / "uninstall.sh"


def test_the_installer_installs_the_updater_and_enables_only_its_timer():
    text = INSTALL.read_text(encoding="utf-8")

    assert "harbor-console-update.service" in text
    assert "systemctl enable --now harbor-console-update.timer" in text
    assert "systemctl enable harbor-console-update.service" not in text
    assert "harbor-console-update.service.d/checkout.conf" in text
    assert "Environment=HARBOR_CHECKOUT=${REPO_ROOT}" in text
    assert 'Environment=PATH=${uv_dir}:' in text


def test_the_installer_writes_the_dropin_before_the_daemon_reload_that_enables_the_timer():
    text = INSTALL.read_text(encoding="utf-8")

    assert text.index("checkout.conf") < text.index("systemctl enable --now harbor-console-update.timer")


def test_uninstall_removes_the_updater():
    text = UNINSTALL.read_text(encoding="utf-8")

    assert "harbor-console-update.timer" in text
    assert "harbor-console-update.service" in text
    assert "harbor-console-update.service.d" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_deploy.py -q`
Expected: 3 failures.

- [ ] **Step 3: Change the installer**

In `deploy/install.sh`, after `UNIT_NAMES=(harbor-console.service harbor-console-web.service)` add:

```bash
# ADR 25: the auto-update pair. Installed with the others, but only the timer
# is enabled; the service is what the timer starts.
UPDATE_UNITS=(harbor-console-update.service harbor-console-update.timer)
UPDATE_DROPIN_DIR="${UNIT_DIR}/harbor-console-update.service.d"
```

Replace the unit install loop

```bash
for unit in "${UNIT_NAMES[@]}"; do
  echo "==> Installing systemd unit to ${UNIT_DIR}/${unit}"
  install -m 0644 "${SCRIPT_DIR}/${unit}" "${UNIT_DIR}/${unit}"
done
systemctl daemon-reload
```

with

```bash
for unit in "${UNIT_NAMES[@]}" "${UPDATE_UNITS[@]}"; do
  echo "==> Installing systemd unit to ${UNIT_DIR}/${unit}"
  install -m 0644 "${SCRIPT_DIR}/${unit}" "${UNIT_DIR}/${unit}"
done

# ADR 25: the updater follows whatever checkout this script was run from,
# and finds uv wherever root found it just now. Rewritten on every install,
# so running the installer from a different checkout moves the timer to it.
# Running it from /opt/harbor-console itself (the rsync target, not a git
# checkout) leaves update.sh refusing with a clear error until it is run
# from a checkout again.
uv_dir=$(dirname "$(command -v uv)")
echo "==> Pointing the updater at ${REPO_ROOT}"
install -d -m 0755 "${UPDATE_DROPIN_DIR}"
cat > "${UPDATE_DROPIN_DIR}/checkout.conf" <<EOF
[Service]
Environment=HARBOR_CHECKOUT=${REPO_ROOT}
Environment=PATH=${uv_dir}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EOF
systemctl daemon-reload
```

Note the heredoc is unquoted on purpose so `${REPO_ROOT}` and `${uv_dir}` expand; the test asserts the literal `${REPO_ROOT}` text in the script source.

After the enable-and-restart loop over `UNIT_NAMES` (the one ending `systemctl restart "${unit}"` / `done`), add:

```bash
# enable --now is right here, unlike above: a timer already running keeps
# its schedule, and the service it fires is the one that runs this script.
echo "==> Enabling harbor-console-update.timer (deploys main every five minutes, ADR 25)"
systemctl enable --now harbor-console-update.timer
```

In the final `for unit in "${UNIT_NAMES[@]}"; do systemctl status ...` block near the end, add after the loop:

```bash
systemctl list-timers harbor-console-update.timer --no-pager || true
```

- [ ] **Step 4: Change the uninstaller**

In `deploy/uninstall.sh`, before the `for unit in "${UNIT_NAMES[@]}"; do echo "==> Stopping and disabling ${unit}"` loop, add:

```bash
echo "==> Stopping and disabling harbor-console-update.timer (ADR 25)"
systemctl disable --now harbor-console-update.timer 2>/dev/null || true
systemctl stop harbor-console-update.service 2>/dev/null || true
```

After the `rm -f "${UNIT_DIR}/${unit}"` loop and before `systemctl daemon-reload`, add:

```bash
for unit in harbor-console-update.service harbor-console-update.timer; do
  echo "==> Removing ${UNIT_DIR}/${unit}"
  rm -f "${UNIT_DIR}/${unit}"
done
rm -rf "${UNIT_DIR}/harbor-console-update.service.d"
```

- [ ] **Step 5: Run the tests and syntax checks**

Run: `uv run pytest -q`
Expected: all pass, including the existing `test_the_installer_ends_with_the_platform_checks`.

Run: `bash -n deploy/install.sh`
Run: `bash -n deploy/uninstall.sh`
Expected: no output.

Run: `git ls-files --eol deploy/install.sh deploy/uninstall.sh`
Expected: `w/lf`.

- [ ] **Step 6: Commit**

```bash
git add deploy/install.sh deploy/uninstall.sh tests/test_deploy.py
git commit -F - <<'EOF'
deploy: install.sh enables the auto-update timer and points it at the checkout it ran from

The drop-in carries the checkout path and uv's directory, rewritten on
every install. uninstall.sh removes all of it.

Assisted-by: Claude Fable 5.1 (Claude Code)
EOF
```

---

### Task 3: ADR 25 and the docs

**Files:**
- Create: `docs/adr/0025-deploy-main-automatically-from-a-host-timer.md`
- Modify: `docs/adr/README.md`
- Modify: `docs/how-docker-cloudflare-and-traefik-work.md` (add a short section before "## Troubleshooting", near line 853)
- Modify: `CLAUDE.md` (the `check.py` bullet mentions `install.sh ends with it`; add one bullet after the "Five behaviours" list)

- [ ] **Step 1: Write the ADR**

Create `docs/adr/0025-deploy-main-automatically-from-a-host-timer.md`:

```markdown
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
`origin/main` as the checkout's owner, exits quietly when `HEAD` already
matches, refuses when the checkout is not on `main`, has modified tracked
files, or has commits the remote lacks, and otherwise `git pull --ff-only`
and hands off to `install.sh`. It never resets, never force-pulls, never
discards work.

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
  accounts that can push to it. The fetch is HTTPS from a public repository
  and needs no credential on the host.
- A refusal (diverged or dirty checkout) leaves the old code running
  correctly and the unit `failed`. Nothing on the page shows that updates
  have stopped flowing. That is the same class of silence ADR 24 closed for
  containers, and a `deploy-fresh` platform check is the obvious follow-up;
  it needs a state file and a definition of "too old" that this decision
  does not make.
- A broken `update.sh` on `main` cannot deploy itself: the service runs the
  installed copy, which only `install.sh` replaces. One manual run from the
  checkout repairs it.
- Rejected: a GitHub Actions workflow deploying over Tailscale SSH (a key
  that is root on the host, held by GitHub); containerising harbor-console
  so Watchtower updates it (the console owns tty1 and the web surface reads
  host sysfs, `/proc` and the Docker socket by design); a webhook listener
  (a new inbound surface on a host whose whole exposure is `:80` and `:443`
  on the tailnet, ADR 15).
```

- [ ] **Step 2: Index it and document the operator's view**

In `docs/adr/README.md`, add after the ADR 24 row:

```markdown
| 0025 | [Deploy main automatically from a host timer](0025-deploy-main-automatically-from-a-host-timer.md) | Accepted |
```

In `docs/how-docker-cloudflare-and-traefik-work.md`, insert before `## Troubleshooting`:

```markdown
## Deploys are automatic (ADR 25)

`harbor-console-update.timer` runs `deploy/update.sh` as root every five
minutes. It fetches `origin/main` as the owner of the checkout `install.sh`
was last run from, and when `main` has moved, fast-forwards and runs
`install.sh`. It refuses, loudly and without touching anything, if the
checkout is not on `main`, has modified tracked files, or has commits the
remote does not; it never resets.

```bash
ssh gte@hpz440 'systemctl list-timers harbor-console-update.timer --no-pager'
ssh gte@hpz440 'journalctl -u harbor-console-update.service -n 50 --no-pager'
```

A quiet journal is a current checkout. A `failed` unit with a refusal
message means someone has to look at the checkout; the old code keeps
running meanwhile. To move the timer to a different checkout, run
`install.sh` from that checkout once.
```

In `CLAUDE.md`, after the bullet beginning "**The verdict file is the only thing the console reads from the web service**" (the last of the five load-bearing behaviours), add:

```markdown
- **Deploys are automatic.** `harbor-console-update.timer` runs `deploy/update.sh` as root every five minutes; it fast-forwards the checkout `install.sh` was last run from and re-runs `install.sh` when `main` has moved, and refuses without touching anything when the checkout is dirty, off `main`, or ahead of the remote ([ADR 25](docs/adr/0025-deploy-main-automatically-from-a-host-timer.md)). A merge to `main` is live on hpz440 within about five minutes; there is no separate deploy step to remember.
```

Also change "Five behaviours of that service are load-bearing" to "Six behaviours of that service are load-bearing".

- [ ] **Step 3: Run the suite and check line endings**

Run: `uv run pytest -q`
Expected: all pass.

Run: `git ls-files --eol CLAUDE.md docs/adr/README.md docs/how-docker-cloudflare-and-traefik-work.md`
Expected: `w/lf`.

- [ ] **Step 4: Commit**

```bash
git add docs/adr/0025-deploy-main-automatically-from-a-host-timer.md docs/adr/README.md docs/how-docker-cloudflare-and-traefik-work.md CLAUDE.md
git commit -F - <<'EOF'
docs: ADR 25, deploy main automatically from a host timer

Assisted-by: Claude Fable 5.1 (Claude Code)
EOF
```
