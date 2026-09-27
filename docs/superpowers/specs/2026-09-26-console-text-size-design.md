# Console text size: 3x glyphs on the tty1 monitor

Date: 2026-09-26
Status: proposed

## The problem

The tty1 dashboard is read from across a room, and the text is too small
to read there. On hpz440 the framebuffer (`radeondrmfb`, monitor on
`card0-DP-1`) runs at 1920x1080 and console-setup installs Ubuntu's default
`Fixed` 8x16 font, so the console is 240 columns by 67 rows. The dashboard
is 19 rows tall and about 50 columns of content, stretched to the full
width by `rich`'s `Panel`: a thin strip of small text across the top of
the screen. The request is text three times its current size.

Two facts bound the solution:

- The kernel console cannot draw a glyph taller than 32 pixels, and the
  largest font Ubuntu ships is Terminus 32x16 -- exactly 2x the current
  8x16. No font alone reaches 3x.
- A framebuffer mode is a 3x lever the font is not. At 1280x720, a 32x16
  glyph covers 48x24 physical pixels on a 1080p panel: 3x in both axes,
  and 80x22 cells. The monitor on DP-1 advertises 1280x720 (checked at
  design time in `/sys/class/drm/card0-DP-1/modes`).

The `rich` renderer needs no change: `Console` reads the tty's size on
every frame, so an 80-column console gets an 80-column panel. The Terminus
32x16 fonts (`Uni2`, `Uni3`, `Lat15` variants) carry every glyph the panel
draws, including the rounded corners `╭╮╰╯`, checked against their PSF
unicode tables at design time.

## Decision

`install.sh` sizes the console as part of taking tty1 over (ADR 4), and
`uninstall.sh` hands it back. Both are host configuration, not application
code, so nothing in `src/` changes. A new ADR 20 records the choice, since
it edits the host's boot configuration and every virtual terminal, not
just tty1.

### Font: console-setup, Terminus 32x16

`install.sh`, in a new `==> Sizing the console (ADR 20)` section placed
right after `getty@tty1` is masked:

1. Back up `/etc/default/console-setup` to
   `/etc/default/console-setup.pre-harbor` once -- only when the backup
   does not already exist, so a re-run never overwrites the original with
   an already-edited copy.
2. Set `FONTFACE="Terminus"` and `FONTSIZE="32x16"` in
   `/etc/default/console-setup`, replacing the existing lines in place
   (`sed -i 's/^FONTFACE=.*/.../'`) and appending either key that is
   missing. Every other key (`CHARMAP`, `CODESET`, `ACTIVE_CONSOLES`) is
   left alone; console-setup picks the `Uni2`/`Uni3`/`Lat15` variant from
   `CODESET` as it does today.
3. Run `setupcon --save --force`. `--save` writes the cached font
   console-setup applies at boot; `--force` is needed because the
   installer's own stdin is an SSH session, not a virtual console, and
   `setupcon` otherwise declines to touch the VTs. The new font is on
   every VT the moment this returns, no reboot needed. `update-initramfs`
   is deliberately not run: the initramfs copy of the font only affects
   the seconds before `console-setup.service` runs, and it is refreshed
   by the next kernel update anyway.

`deploy/harbor-console.service` gains `After=console-setup.service`.
`console-setup.service` is wanted by `multi-user.target` with no ordering
against the dashboard today, so without this the first frame after boot
can render at the 8x16 geometry and snap to the new size a second later.
`rich` would recover on its own; the ordering just makes the first frame
right.

### Mode: GRUB drop-in, 1280x720

Ubuntu's `grub-mkconfig` sources every `/etc/default/grub.d/*.cfg` after
`/etc/default/grub`. `install.sh` writes
`/etc/default/grub.d/harbor-console.cfg`:

```sh
# Harbor Console (ADR 20): 1280x720 with a 32x16 console font gives 3x
# glyphs on a 1080p monitor. Removed by deploy/uninstall.sh.
GRUB_CMDLINE_LINUX_DEFAULT="${GRUB_CMDLINE_LINUX_DEFAULT} video=1280x720"
```

and runs `update-grub` only when the file was created or its content
changed, so an unchanged re-run does not regenerate the GRUB config.
`video=1280x720` with no connector prefix is the DRM default for every
connector, so it follows the monitor if it moves from DP-1 to the DVI
port. A monitor that does not advertise the mode makes DRM ignore the
option and keep its preferred mode, which is the failure that leaves the
console readable at 2x rather than blank.

The mode takes effect at the next boot. `install.sh` never reboots. After
writing the drop-in it checks `/proc/cmdline` for `video=1280x720` and,
when absent, prints one notice at the end of the run:

```
Reboot to switch the console to 1280x720; until then the text is 2x, not 3x.
```

On a host with no `update-grub` (not Ubuntu, or not GRUB) the section
prints a warning naming the kernel parameter to add by hand and continues;
the font step above still applies.

### Reversal: uninstall.sh

After restoring the `getty@tty1` login prompt:

1. Remove `/etc/default/grub.d/harbor-console.cfg` and run `update-grub`
   if the file existed. The running kernel keeps the mode until reboot;
   say so.
2. If `/etc/default/console-setup.pre-harbor` exists, move it back over
   `/etc/default/console-setup` and run `setupcon --save --force`, which
   puts the original font on every VT immediately. If the backup does not
   exist the font is left as it is, with a warning -- the script does not
   guess what the host had before.

### Geometry and its limit

| | Today | After |
|---|---|---|
| Framebuffer | 1920x1080 | 1280x720 |
| Font | Fixed 8x16 | Terminus 32x16 |
| Cells | 240x67 | 80x22 |
| Glyph on the panel | 8x16 px | 24x48 px (3x) |
| Dashboard | 19 of 67 rows | 19 of 22 rows |

Three spare rows. The dashboard grows one row per local filesystem, remote
mount, volume group with slack, stray device and GPU, so a host with three
more of those than hpz440 has today overflows, and `rich`'s alternate
screen crops the bottom rows -- IPv4, container count and clock -- with no
message. That is a visible failure on the monitor, and the fix is a step
down to Terminus 28x14 (91x25 cells at 1280x720, 2.6x) in `install.sh`,
not application code that compresses rows. The panel is not made narrower
or taller in this change; 80 columns is already close to its content.

## Testing

There are no tests for the deploy scripts and this change adds none; the
scripts are verified on the host, as every install.sh change has been:

1. `sudo bash /srv/harbor-console/deploy/install.sh` (after `git pull` in
   `/srv/harbor-console`). Expect the font to change on the attached
   monitor during the run and the reboot notice at the end.
2. `grep FONT /etc/default/console-setup`, `cat /etc/default/grub.d/harbor-console.cfg`,
   `grep video= /boot/grub/grub.cfg`.
3. Reboot when convenient. Then `cat /sys/class/graphics/fb0/virtual_size`
   reads `1280,720`, and `stty size </dev/tty1` (as root) reads `22 80`.
4. The dashboard fills the monitor at 3x, all 19 rows present, clock
   ticking.
5. Re-run `install.sh`: no `update-grub` run, no reboot notice, no change.
6. `uninstall.sh` on a scratch host or VM, or reading: drop-in gone,
   `console-setup` byte-identical to the backup, font back at 8x16
   without a reboot.

## Out of scope

- Any `rich` layout change (padding, larger type effects, hiding rows).
- Per-VT font: console-setup's `ACTIVE_CONSOLES` sets every VT, and the
  admin logins on tty2-tty6 get the big font too. Acceptable; they are
  read on the same monitor.
- A different mode per host. One host ships; the mode is a constant in
  the drop-in, in the spirit of ADR 15's fixed port.
