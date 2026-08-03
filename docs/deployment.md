# Deploying Harbor Console

Harbor Console runs as a systemd service that takes over the physical console
(`tty1`) at boot and shows the dashboard. This guide covers install, update,
and removal.

## Prerequisites

- A systemd-based Linux host.
- Root access (`sudo`).
- [`uv`](https://docs.astral.sh/uv/) and `rsync` installed on the host.
- Docker installed — the service user joins the `docker` group, and the unit will not start without it.

## Install

Copy this repository to the server, then run:

```bash
sudo deploy/install.sh
```

The installer is idempotent — re-run it any time to update.

### What it changes

- Copies the repo to `/opt/harbor-console` and builds `.venv` with `uv sync`.
  If the host has no Python 3.13+, `uv` fetches a managed interpreter into
  `/opt/harbor-console/.uv-python` (not root's home) so the unprivileged `harbor`
  user — running under `ProtectHome=yes` — can execute it.
- Creates a `harbor` system user (added to the `docker` group) that owns
  `/opt/harbor-console` and runs the service.
- Installs `/etc/systemd/system/harbor-console.service`.
- Masks `getty@tty1.service` (removes the login prompt on **tty1 only**).
- Enables and starts `harbor-console.service`.

## Admin access (important)

Masking `getty@tty1` removes the interactive login on `tty1` only. Virtual
terminals **tty2–tty6 keep their normal logins** — reach them with
Ctrl+Alt+F2 … F6 — and SSH is unaffected. You cannot be locked out at the
physical keyboard, even if the dashboard crash-loops.

## Update

Re-run the installer from an **updated checkout of the repository**:

```bash
sudo deploy/install.sh
```

It re-syncs `/opt/harbor-console`, rebuilds the virtualenv with `uv sync`, and
restarts the service so the new code takes effect. Note: `/opt/harbor-console`
is a copy, not a git clone — pull updates in your checkout, then re-run the
installer.

## Uninstall

```bash
sudo deploy/uninstall.sh          # restores the tty1 login prompt
sudo deploy/uninstall.sh --purge  # also removes /opt/harbor-console and the harbor user
```

## Troubleshooting

- Logs: `journalctl -u harbor-console -b`
- Service state: `systemctl status harbor-console`
- Nothing on the monitor: confirm the unit is active and `getty@tty1` is
  masked (`systemctl is-enabled getty@tty1`); switch to the console with
  Ctrl+Alt+F1.
- `status=203/EXEC` / `Permission denied` executing `.venv/bin/harbor-console`:
  the venv's Python points somewhere the `harbor` user can't reach (e.g. a
  `uv`-managed interpreter under `/root`, which `ProtectHome=yes` also hides). Re-run
  the installer — it pins the interpreter under `/opt/harbor-console/.uv-python`.
  Verify with `readlink -f /opt/harbor-console/.venv/bin/python` (must resolve
  inside `/opt/harbor-console`).

## Smoke test (run once on the target)

1. Validate the installed unit: `systemd-analyze verify /etc/systemd/system/harbor-console.service`
   → no output, exit 0.
2. Reboot → the dashboard appears on the attached monitor (tty1).
3. Confirm the Docker container count is correct (not stuck at 0) — verifies the
   `harbor` user's `docker`-group access.
4. `sudo systemctl kill harbor-console` → the dashboard returns within ~2s.
5. Press Ctrl+Alt+F2 → a normal login prompt is available.
6. `sudo deploy/uninstall.sh` → the login prompt returns on tty1.
