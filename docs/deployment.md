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
