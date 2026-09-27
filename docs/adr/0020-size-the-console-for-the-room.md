# 20. Size the console for the room: Terminus 32x16 at 1280x720

Date: 2026-09-26

## Status

Accepted

## Context

The tty1 dashboard (ADR 4) is read from across a room on a 1080p monitor.
Ubuntu's console-setup installs an 8x16 font, so the console is 240x67
cells and the 19-row dashboard is a strip of small text along the top of
the screen. The request was text three times its current size.

The kernel console cannot draw a glyph taller than 32 pixels, and the
largest font Ubuntu ships is Terminus 32x16: exactly 2x. The remaining
1.5x has to come from the framebuffer mode. At 1280x720 on a 1080p panel
a Terminus 32x16 glyph (16 pixels wide, 32 tall) covers 24 by 48 physical
pixels -- 3x the 8-by-16 default in both axes -- and the
console is 80x22 cells. The monitor on hpz440 advertises 1280x720, and the
Terminus 32x16 fonts carry every glyph the panel draws, rounded corners
included (both checked at design time).

`rich` reads the tty's size on every frame, so nothing in the renderer
needs to know any of this.

## Decision

We will make console sizing part of taking tty1 over. `install.sh` backs
up `/etc/default/console-setup` once (to `console-setup.pre-harbor`), sets
`FONTFACE="Terminus"` and `FONTSIZE="32x16"`, and applies them on every VT
immediately with `setupcon --save --force`. It writes
`/etc/default/grub.d/harbor-console.cfg`, appending `video=1280x720` to
`GRUB_CMDLINE_LINUX_DEFAULT`, and runs `update-grub` when that file is new
or changed. The mode takes effect at the next boot; the script says so and
never reboots. `harbor-console.service` orders itself after
`console-setup.service` so the first frame after boot is drawn at the
right geometry. `uninstall.sh` removes the drop-in, regenerates the GRUB
config, and moves the backup back.

The mode is a constant, not configuration, in the spirit of ADR 15's fixed
port: one host ships, and the console is sized for its monitor.

## Consequences

- The text is 3x after one reboot, 2x from the moment `install.sh` runs.
- Every VT gets the font, so admin logins on tty2-tty6 are large too. They
  are read on the same monitor; acceptable.
- 22 rows leave 3 spare over the current dashboard. That is today's count,
  not a bound: at 80 columns a long mountpoint label with a long note wraps
  its value onto a second row, so one entry can cost two. The dashboard
  grows one row per local filesystem, remote mount, volume group with
  slack, stray device and GPU; past 22 rows `rich` crops the bottom of the
  panel with no message. That is visible on the monitor, and the remedy is
  a smaller font in `install.sh` (Terminus 28x14 gives 91x25 cells, 2.6x),
  not application code that compresses rows.
- A monitor that does not advertise 1280x720 does not make DRM fall back:
  the kernel synthesizes timings for the requested mode, which the monitor
  may or may not sync to. Check the connector's mode list in
  `/sys/class/drm/*/modes` before shipping this to another host; hpz440's
  DP-1 advertises it.
- `install.sh` now edits a file outside `/opt` and `/etc/systemd` that it
  did not create. The once-only backup and the restore in `uninstall.sh`
  are what keep that reversible.
- Rejected: a manual one-time host change (lost on a rebuilt host); a
  kernel-only `fbcon=font:TER16x32` (console-setup resets the font at
  boot, so it would have to be disabled); any `rich` layout change
  (cannot change glyph size).
