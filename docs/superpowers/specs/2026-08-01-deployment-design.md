# Harbor Console — Deployment Design

Date: 2026-08-01
Status: Approved (design), pending implementation

## Purpose

Harbor Console's first release criterion is that it **automatically starts** and
replaces the default Linux login console (`founding_document.txt`). The
application code that displays the metrics is complete and tested; the missing
half of the MVP is the mechanism that runs it on the attached monitor at boot.

This spec defines that deployment mechanism: a systemd service that owns the
physical console (`tty1`) plus scripts to install and reverse it.

Out of scope: any change to the Python application, packaging to PyPI,
multi-host orchestration, or configuration options. Those remain deferred per
the founding document.

## Decisions (settled during brainstorming)

1. **Mechanism:** a dedicated systemd service owns `tty1` and runs the dashboard
   directly. The login prompt on `tty1` (`getty@tty1.service`) is masked.
2. **Run-as user:** `root` (has Docker socket and `/` disk access with no extra
   setup; fits KISS/MVP).
3. **Install layout:** the repo is placed at `/opt/harbor-console`, with a `uv`
   virtualenv built there. The service runs the venv's console script.
4. **Delivery form:** a committed unit file plus idempotent `install.sh` and
   `uninstall.sh`, and a `docs/deployment.md` guide.

## Artifacts

```
deploy/
  harbor-console.service    # systemd unit
  install.sh                # idempotent installer (run as root)
  uninstall.sh              # full reversal (run as root)
docs/
  deployment.md             # prerequisites, usage, update, troubleshoot, smoke test
```

No changes to `src/` or `tests/`.

## Component 1 — systemd unit (`deploy/harbor-console.service`)

```ini
[Unit]
Description=Harbor Console dashboard
After=systemd-user-sessions.service network-online.target
Wants=network-online.target
Conflicts=getty@tty1.service

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

Rationale for the non-obvious directives:

- **`TTYPath=/dev/tty1` + `StandardInput=tty-force` + `StandardOutput=tty`** —
  hand the physical console to the process so `rich`'s full-screen `Live`
  (`screen=True`) renders on the attached monitor. `tty-force` takes the TTY even
  if something else holds it.
- **`Environment=TERM=linux`** — under systemd there is no inherited `TERM`;
  without it `rich` cannot render correctly on the console.
- **`Restart=always`, `RestartSec=2`** — satisfies "never crashes during normal
  operation." Every exit is restarted within ~2s, including the clean
  `KeyboardInterrupt`→`return 0` path, so the dashboard always comes back.
- **`Conflicts=getty@tty1.service`** — declares the mutual exclusion with the
  login prompt; the installer additionally masks getty so it never starts on
  `tty1`.
- **`Wants=network-online.target` (not `Requires`)** — a slow or absent network
  must not block boot. `get_ipv4_address()` falls back to `127.0.0.1` and the
  Docker count to `0`, and both self-heal on the next one-second refresh once the
  network is up.
- **`TTYReset=yes`, `TTYVHangup=yes`** — reset the console and hang up any stray
  sessions on start/stop so the display is clean.

## Component 2 — installer (`deploy/install.sh`)

Run as root from a checkout of the repo. Idempotent — safe to re-run to update.

Steps:

1. Assert `EUID == 0`; exit with a clear message if not root.
2. Assert `uv` is on `PATH`; exit with an install hint if missing.
3. Sync the repo into `/opt/harbor-console` (rsync from the script's own repo
   root, excluding `.git`, `.venv`, `__pycache__`, `.pytest_cache`).
4. Run `uv sync` inside `/opt/harbor-console` to build `.venv` with the runtime
   dependencies only (`rich`, `psutil`). `pytest` lives in the `dev` optional
   extra, which `uv sync` does not install unless `--extra dev` is passed, so no
   extra flag is needed to exclude it.
5. Install `harbor-console.service` to `/etc/systemd/system/` and
   `systemctl daemon-reload`.
6. `systemctl mask getty@tty1.service` — disable the login prompt on `tty1`
   only.
7. `systemctl enable --now harbor-console.service`.
8. Print `systemctl status harbor-console.service --no-pager` for confirmation.

Idempotency: rsync, `uv sync`, `mask`, and `enable --now` are all convergent, so
re-running updates the deployment without side effects.

Behavior on error: the script uses `set -euo pipefail` and fails fast with a
descriptive message at the first failing step; a partial run can be corrected by
fixing the cause and re-running.

## Component 3 — uninstaller (`deploy/uninstall.sh`)

Run as root. Reverses the installer:

1. Assert `EUID == 0`.
2. `systemctl disable --now harbor-console.service` (ignore "not loaded").
3. Remove `/etc/systemd/system/harbor-console.service`; `systemctl daemon-reload`.
4. `systemctl unmask getty@tty1.service` and `systemctl start getty@tty1.service`
   to restore the login prompt on `tty1`.
5. Leave `/opt/harbor-console` in place by default; remove it only when called
   with `--purge`.

## Component 4 — documentation (`docs/deployment.md`)

Contents:

- **Prerequisites:** a systemd-based Linux host, `uv` installed, root access.
- **Install:** copy the repo to the server, run `sudo deploy/install.sh`.
- **What it changes:** files written (`/opt/harbor-console`,
  `/etc/systemd/system/harbor-console.service`) and units touched
  (`harbor-console.service` enabled, `getty@tty1` masked).
- **Update:** re-run `install.sh` (or `git pull` in `/opt/harbor-console` then
  `uv sync` and `systemctl restart harbor-console`).
- **Uninstall:** `sudo deploy/uninstall.sh` (add `--purge` to remove `/opt`).
- **Troubleshooting:** `journalctl -u harbor-console -b`, and the admin-access
  note below.
- **Smoke test:** the checklist from the Testing section.

### Console-access safety note (must be prominent)

Masking `getty@tty1` removes the interactive login on `tty1` only. Virtual
terminals **tty2–tty6 keep their normal logins** (reach them with
Ctrl+Alt+F2 … F6). An administrator at the physical keyboard is therefore never
locked out, even if the dashboard crash-loops. This note appears in both
`install.sh` output and `deployment.md`.

## Testing strategy

Deployment cannot be unit-tested on the Windows development host, and systemd is
unavailable there. This is an accepted, documented gap — the same "never run on
real Linux" gap noted for the app itself.

- **Static validation (CI-capable):**
  - `shellcheck deploy/install.sh deploy/uninstall.sh`.
  - `systemd-analyze verify deploy/harbor-console.service` (runs on a Linux host
    or in Linux CI).
- **Manual smoke test on the target server** (documented in `deployment.md`, run
  once):
  1. Reboot → the dashboard appears on the attached monitor (`tty1`).
  2. `sudo systemctl kill harbor-console` → the dashboard returns within ~2s.
  3. Ctrl+Alt+F2 → a normal login prompt is still available.
  4. `sudo deploy/uninstall.sh` → the login prompt returns on `tty1`.
- **Application tests** (`uv run pytest`) continue to cover the Python code and
  are unaffected by this work.

Full verification requires the target machine; the spec does not claim the
deployment works until that smoke test has been run on real hardware.

## Acceptance criteria

- `deploy/harbor-console.service`, `deploy/install.sh`, `deploy/uninstall.sh`, and
  `docs/deployment.md` exist and are committed.
- `shellcheck` passes on both scripts.
- On a systemd Linux host: `install.sh` results in the dashboard rendering on
  `tty1` at boot and restarting within ~2s after a kill; `uninstall.sh` fully
  restores the original `tty1` login prompt.
- tty2–tty6 remain interactive logins throughout.

## Follow-up (not in this spec)

- Record the deployment mechanism as an ADR under `docs/adr/` once implemented.
- Commit a `uv.lock` for reproducible installs (tracked separately from this
  spec; noted because `install.sh` relies on `uv sync`).
