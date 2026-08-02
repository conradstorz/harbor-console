# Harbor Console Deployment Implementation Plan

> **Amended 2026-08-01:** the service runs as a dedicated `harbor` user with
> conservative systemd hardening rather than `root` — see
> [ADR 5](../../adr/0005-run-as-harbor-user.md) and
> [the dedicated-user increment plan](2026-08-01-deployment-nonroot.md). The
> `User=root` and install/uninstall snippets below reflect the original design.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Harbor Console start automatically on boot and take over the physical console (`tty1`), fulfilling the MVP's "automatically starts" release criterion.

**Architecture:** A dedicated systemd service owns `/dev/tty1` and runs the existing `harbor-console` entry point from a `uv` virtualenv at `/opt/harbor-console`. The normal login prompt on `tty1` (`getty@tty1.service`) is masked; tty2–tty6 keep their logins. Idempotent `install.sh` / `uninstall.sh` scripts and a `docs/deployment.md` guide wrap the whole thing. No application code changes.

**Tech Stack:** systemd unit files, Bash (installer/uninstaller), `uv`, `rsync`, `rich`/`psutil` (existing runtime deps).

## Global Constraints

- Target host is systemd-based Linux; the service runs as `User=root`.
- Install location is `/opt/harbor-console`; the unit's `ExecStart` is `/opt/harbor-console/.venv/bin/harbor-console`.
- Mask **only** `getty@tty1.service` — never touch tty2–tty6.
- The unit must set `Environment=TERM=linux` and `Restart=always` with `RestartSec=2`.
- Dependencies are installed with `uv sync` (no `--extra dev`; `pytest` must not be installed on the server).
- All scripts start with `#!/usr/bin/env bash` and `set -euo pipefail`, and fail fast with a descriptive message when a prerequisite is missing.
- **Verification reality:** systemd/tty behavior cannot be exercised on the Windows dev host. The gate runnable during implementation is `bash -n` (syntax). `shellcheck`, `systemd-analyze verify`, and the manual smoke test are Linux/target/CI gates — record them as done-on-target, do not fake them.

---

### Task 1: systemd unit file

**Files:**
- Create: `deploy/harbor-console.service`

**Interfaces:**
- Consumes: the existing console script `/opt/harbor-console/.venv/bin/harbor-console` (produced by `pyproject.toml`'s `[project.scripts]`, installed by Task 2's `uv sync`).
- Produces: unit name `harbor-console.service` with `ExecStart=/opt/harbor-console/.venv/bin/harbor-console`; consumed by Tasks 2, 3, 4.

- [ ] **Step 1: Create the unit file**

Create `deploy/harbor-console.service`:

```ini
[Unit]
Description=Harbor Console dashboard
After=systemd-user-sessions.service network-online.target
Wants=network-online.target
Conflicts=getty@tty1.service
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/opt/harbor-console/.venv/bin/harbor-console
Restart=always
RestartSec=2
User=root
TTYPath=/dev/tty1
StandardInput=tty-force
StandardOutput=tty
StandardError=journal
TTYReset=yes
TTYVHangup=yes
Environment=TERM=linux

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Sanity-check required directives (runnable on dev host)**

Run (Git Bash):
```bash
grep -qE '^ExecStart=/opt/harbor-console/\.venv/bin/harbor-console$' deploy/harbor-console.service \
  && grep -qE '^Conflicts=getty@tty1\.service$' deploy/harbor-console.service \
  && grep -qE '^Environment=TERM=linux$' deploy/harbor-console.service \
  && grep -qE '^Restart=always$' deploy/harbor-console.service \
  && echo "unit-ok"
```
Expected output: `unit-ok`

- [ ] **Step 3: Note the deferred gate**

`systemd-analyze verify deploy/harbor-console.service` must be run on the Linux target/CI (it is unavailable on the dev host). Expected there: no output and exit code 0. Do not mark this as passed until it is actually run on Linux; record it in the smoke test (Task 4).

- [ ] **Step 4: Commit**

```bash
git add deploy/harbor-console.service
git commit -m "feat(deploy): add systemd unit for tty1 dashboard service"
```

---

### Task 2: installer script

**Files:**
- Create: `deploy/install.sh`

**Interfaces:**
- Consumes: `deploy/harbor-console.service` (Task 1); the repo root two levels up from the script (`deploy/..`).
- Produces: an installed, enabled `harbor-console.service`; `/opt/harbor-console` populated with a built `.venv`; `getty@tty1` masked. Reversed by Task 3.

- [ ] **Step 1: Create the installer**

Create `deploy/install.sh`:

```bash
#!/usr/bin/env bash
# Harbor Console installer — sets up the tty1 dashboard service.
# Run as root from a checkout of the repository. Idempotent.
set -euo pipefail

INSTALL_DIR=/opt/harbor-console
UNIT_NAME=harbor-console.service
UNIT_DEST=/etc/systemd/system/${UNIT_NAME}

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

require_root() {
  if [[ ${EUID} -ne 0 ]]; then
    echo "Error: install.sh must be run as root (try: sudo $0)" >&2
    exit 1
  fi
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: '$1' is not on PATH. $2" >&2
    exit 1
  fi
}

require_root
require_cmd uv "Install it first: https://docs.astral.sh/uv/"
require_cmd rsync "Install it with your package manager (e.g. apt install rsync)."

echo "==> Syncing repository to ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
rsync -a --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '*.egg-info' \
  "${REPO_ROOT}/" "${INSTALL_DIR}/"

echo "==> Building virtualenv with uv sync"
( cd "${INSTALL_DIR}" && uv sync )

echo "==> Installing systemd unit to ${UNIT_DEST}"
install -m 0644 "${SCRIPT_DIR}/${UNIT_NAME}" "${UNIT_DEST}"
systemctl daemon-reload

echo "==> Masking getty@tty1 (disables the login prompt on tty1 only)"
systemctl mask getty@tty1.service

echo "==> Enabling ${UNIT_NAME} and (re)starting it to load current code"
systemctl enable "${UNIT_NAME}"
systemctl restart "${UNIT_NAME}"

echo
echo "Harbor Console is installed. tty1 now shows the dashboard."
echo "Admin logins remain on tty2-tty6 (Ctrl+Alt+F2 ... F6) and via SSH."
echo
systemctl status "${UNIT_NAME}" --no-pager || true
```

- [ ] **Step 2: Verify syntax (runnable on dev host)**

Run:
```bash
bash -n deploy/install.sh && echo "syntax-ok"
```
Expected output: `syntax-ok`

- [ ] **Step 3: Note the deferred gate**

`shellcheck deploy/install.sh` must be run on Linux/CI (unavailable on the dev host). Expected there: no findings, exit code 0.

- [ ] **Step 4: Commit**

```bash
git add deploy/install.sh
git commit -m "feat(deploy): add idempotent install.sh"
```

---

### Task 3: uninstaller script

**Files:**
- Create: `deploy/uninstall.sh`

**Interfaces:**
- Consumes: the artifacts Task 2 creates (`harbor-console.service`, masked `getty@tty1`, `/opt/harbor-console`).
- Produces: original state restored — `getty@tty1` unmasked and started, unit removed. `--purge` additionally removes `/opt/harbor-console`.

- [ ] **Step 1: Create the uninstaller**

Create `deploy/uninstall.sh`:

```bash
#!/usr/bin/env bash
# Harbor Console uninstaller — reverses install.sh.
# Run as root. Pass --purge to also remove /opt/harbor-console.
set -euo pipefail

INSTALL_DIR=/opt/harbor-console
UNIT_NAME=harbor-console.service
UNIT_DEST=/etc/systemd/system/${UNIT_NAME}

PURGE=0
if [[ "${1:-}" == "--purge" ]]; then
  PURGE=1
fi

if [[ ${EUID} -ne 0 ]]; then
  echo "Error: uninstall.sh must be run as root (try: sudo $0)" >&2
  exit 1
fi

echo "==> Stopping and disabling ${UNIT_NAME}"
systemctl disable --now "${UNIT_NAME}" 2>/dev/null || true

echo "==> Removing ${UNIT_DEST}"
rm -f "${UNIT_DEST}"
systemctl daemon-reload

echo "==> Restoring the login prompt on tty1"
systemctl unmask getty@tty1.service 2>/dev/null || true
systemctl start getty@tty1.service 2>/dev/null || true

if [[ ${PURGE} -eq 1 ]]; then
  echo "==> Purging ${INSTALL_DIR}"
  rm -rf "${INSTALL_DIR}"
else
  echo "==> Leaving ${INSTALL_DIR} in place (use --purge to remove it)"
fi

echo "Harbor Console has been uninstalled."
```

- [ ] **Step 2: Verify syntax (runnable on dev host)**

Run:
```bash
bash -n deploy/uninstall.sh && echo "syntax-ok"
```
Expected output: `syntax-ok`

- [ ] **Step 3: Note the deferred gate**

`shellcheck deploy/uninstall.sh` on Linux/CI — expected: no findings, exit code 0.

- [ ] **Step 4: Commit**

```bash
git add deploy/uninstall.sh
git commit -m "feat(deploy): add uninstall.sh with --purge"
```

---

### Task 4: deployment documentation

**Files:**
- Create: `docs/deployment.md`

**Interfaces:**
- Consumes: all three `deploy/` artifacts and their documented behavior.
- Produces: the operator-facing guide, including the smoke-test checklist referenced by the spec's acceptance criteria.

- [ ] **Step 1: Write the guide**

Create `docs/deployment.md`:

```markdown
# Deploying Harbor Console

Harbor Console runs as a systemd service that takes over the physical console
(`tty1`) at boot and shows the dashboard. This guide covers install, update,
and removal.

## Prerequisites

- A systemd-based Linux host.
- Root access (`sudo`).
- [`uv`](https://docs.astral.sh/uv/) and `rsync` installed on the host.

## Install

Copy this repository to the server, then run:

```bash
sudo deploy/install.sh
```

The installer is idempotent — re-run it any time to update.

### What it changes

- Copies the repo to `/opt/harbor-console` and builds `.venv` with `uv sync`.
- Installs `/etc/systemd/system/harbor-console.service`.
- Masks `getty@tty1.service` (removes the login prompt on **tty1 only**).
- Enables and starts `harbor-console.service`.

## Admin access (important)

Masking `getty@tty1` removes the interactive login on `tty1` only. Virtual
terminals **tty2–tty6 keep their normal logins** — reach them with
Ctrl+Alt+F2 … F6 — and SSH is unaffected. You cannot be locked out at the
physical keyboard, even if the dashboard crash-loops.

## Update

Re-run `sudo deploy/install.sh`, or manually:

```bash
cd /opt/harbor-console
sudo git pull        # if deployed from a clone
sudo uv sync
sudo systemctl restart harbor-console
```

## Uninstall

```bash
sudo deploy/uninstall.sh          # restores the tty1 login prompt
sudo deploy/uninstall.sh --purge  # also removes /opt/harbor-console
```

## Troubleshooting

- Logs: `journalctl -u harbor-console -b`
- Service state: `systemctl status harbor-console`
- Nothing on the monitor: confirm the unit is active and `getty@tty1` is
  masked (`systemctl is-enabled getty@tty1`); switch to the console with
  Ctrl+Alt+F1.

## Smoke test (run once on the target)

1. Validate the unit: `systemd-analyze verify deploy/harbor-console.service`
   → no output, exit 0.
2. Reboot → the dashboard appears on the attached monitor (tty1).
3. `sudo systemctl kill harbor-console` → the dashboard returns within ~2s.
4. Press Ctrl+Alt+F2 → a normal login prompt is available.
5. `sudo deploy/uninstall.sh` → the login prompt returns on tty1.
```

- [ ] **Step 2: Verify the doc renders and links resolve**

Run:
```bash
grep -q "Admin access (important)" docs/deployment.md \
  && grep -q "systemd-analyze verify" docs/deployment.md \
  && echo "doc-ok"
```
Expected output: `doc-ok`

- [ ] **Step 3: Commit**

```bash
git add docs/deployment.md
git commit -m "docs: add deployment guide with smoke test"
```

---

### Task 5: record the deployment decision as an ADR

**Files:**
- Create: `docs/adr/0004-systemd-tty1-service.md`
- Modify: `docs/adr/README.md` (add a table row)

**Interfaces:**
- Consumes: the ADR conventions established in `docs/adr/README.md` and `docs/adr/template.md`.
- Produces: ADR 0004, closing the spec's follow-up item so the repo stays consistent with its own decision-record practice.

- [ ] **Step 1: Write the ADR**

Create `docs/adr/0004-systemd-tty1-service.md`:

```markdown
# 4. Run as a systemd service that owns tty1

Date: 2026-08-01

## Status

Accepted

## Context

The MVP must "automatically start" and "replace the default Linux login
console" (`founding_document.txt`). The application renders a full-screen
`rich` dashboard and must appear on the server's attached monitor at boot,
restart if it ever exits, and never lock an administrator out of the machine.

Options considered: a getty autologin override on tty1 whose shell profile
launches the app, versus a dedicated systemd service that owns tty1 directly.

## Decision

Ship a dedicated `harbor-console.service` that binds `/dev/tty1`
(`TTYPath` + `StandardInput=tty-force`), runs the app as root from a `uv`
virtualenv at `/opt/harbor-console`, and uses `Restart=always`. The installer
masks `getty@tty1.service` so the login prompt does not fight for the console.
Only tty1 is masked; tty2–tty6 remain normal logins.

## Consequences

- Clean lifecycle: journald logging, `Restart=always` recovery, and no shell
  profile indirection.
- No interactive login on tty1; admins use tty2–tty6 or SSH. This is the
  intended "replace the login console" behavior, and keeping the other VTs
  ensures no lockout.
- Requires `uv` and `rsync` on the target host (checked by `install.sh`).
- The getty-autologin alternative was rejected as more moving parts with a
  messier restart story.
```

- [ ] **Step 2: Add the ADR to the index**

In `docs/adr/README.md`, add this row to the Records table, immediately after the `0003` row:

```markdown
| 0004 | [Run as a systemd service that owns tty1](0004-systemd-tty1-service.md) | Accepted |
```

- [ ] **Step 3: Verify the link and index entry**

Run:
```bash
grep -q "0004-systemd-tty1-service.md" docs/adr/README.md \
  && test -f docs/adr/0004-systemd-tty1-service.md \
  && echo "adr-ok"
```
Expected output: `adr-ok`

- [ ] **Step 4: Commit**

```bash
git add docs/adr/0004-systemd-tty1-service.md docs/adr/README.md
git commit -m "docs(adr): record tty1 systemd service decision"
```

---

## Final verification (on the Linux target, after all tasks)

These close the spec's acceptance criteria and cannot be run on the dev host:

- [ ] `shellcheck deploy/install.sh deploy/uninstall.sh` → no findings.
- [ ] `systemd-analyze verify deploy/harbor-console.service` → exit 0.
- [ ] Run the 5-step smoke test in `docs/deployment.md` on real hardware; all steps pass, and tty2–tty6 stay interactive throughout.

Only after the smoke test passes on the target may the deployment be called complete — do not claim success from the dev host.
