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
