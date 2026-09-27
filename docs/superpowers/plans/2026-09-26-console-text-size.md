# Console Text Size Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the tty1 dashboard's text three times its current size on the attached 1080p monitor, by host configuration that `install.sh` applies and `uninstall.sh` reverses.

**Architecture:** No application code changes. `install.sh` gains one section that sets the console font to Terminus 32x16 through console-setup (live, no reboot) and writes a GRUB drop-in adding `video=1280x720` to the kernel command line (next boot). `uninstall.sh` removes the drop-in and restores the backed-up console-setup file. The dashboard unit orders itself after `console-setup.service`. ADR 20 records the decision.

**Tech Stack:** bash (`deploy/install.sh`, `deploy/uninstall.sh`), systemd unit, Ubuntu 24.04 `console-setup` (`setupcon`) and `update-grub`.

Spec: `docs/superpowers/specs/2026-09-26-console-text-size-design.md`. Read it first.

## Global Constraints

- No change under `src/` or `tests/`; `rich` already sizes the panel to the tty.
- Font: `FONTFACE="Terminus"`, `FONTSIZE="32x16"` in `/etc/default/console-setup`; other keys untouched.
- Mode: kernel parameter `video=1280x720` (no connector prefix, no refresh rate) via `/etc/default/grub.d/harbor-console.cfg`.
- Backup path: `/etc/default/console-setup.pre-harbor`, written once, never overwritten.
- `install.sh` never reboots; it prints exactly this line when `/proc/cmdline` lacks the parameter: `Reboot to switch the console to 1280x720; until then the text is 2x, not 3x.`
- `update-grub` runs only when the drop-in was created or changed; missing `update-grub` is a warning, not an exit.
- `install.sh` is `set -euo pipefail`; every command that may legitimately fail must be guarded, and no `yes |` pipelines (see the ufw comment in the file for why).
- Scripts are not executable in the checkout; syntax-check with `bash -n`. There are no automated tests for deploy scripts; on-host verification is in the spec's Testing section and is the user's step, not the implementer's.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

### Task 1: install.sh sizes the console; unit waits for console-setup

**Files:**
- Modify: `deploy/install.sh:152-153` (insert after the getty mask), `deploy/install.sh:334-338` (final notice)
- Modify: `deploy/harbor-console.service:3-4` (`After=`)

**Interfaces:**
- Produces: the file `/etc/default/grub.d/harbor-console.cfg` and the backup `/etc/default/console-setup.pre-harbor`, both of which Task 2 removes/restores by these exact paths.

- [ ] **Step 1: Confirm the insertion points**

Run: `grep -n "Masking getty@tty1\|Harbor Console is installed" deploy/install.sh`
Expected: two lines, near 152 and 335.

- [ ] **Step 2: Add the console-sizing section after the getty mask**

Directly after these two existing lines:

```bash
echo "==> Masking getty@tty1 (disables the login prompt on tty1 only)"
systemctl mask getty@tty1.service
```

insert:

```bash
# ADR 20: the dashboard is read from across a room. The largest kernel
# console font is 32x16 (2x Ubuntu's 8x16 default); 1280x720 on a 1080p
# panel supplies the other 1.5x, so a glyph covers 24x48 physical pixels --
# 3x in both axes -- and the console is 80x22 cells against a 19-row
# dashboard. Font applies now on every VT; the mode needs a reboot, which
# this script never performs.
echo "==> Sizing the console (ADR 20)"
CONSOLE_SETUP=/etc/default/console-setup
CONSOLE_SETUP_BACKUP=/etc/default/console-setup.pre-harbor
GRUB_DROPIN=/etc/default/grub.d/harbor-console.cfg
CONSOLE_VIDEO_MODE=1280x720
console_reboot_needed=0
if [[ -f "${CONSOLE_SETUP}" ]]; then
  # Once: a re-run must not overwrite the original with an edited copy.
  if [[ ! -f "${CONSOLE_SETUP_BACKUP}" ]]; then
    cp -p "${CONSOLE_SETUP}" "${CONSOLE_SETUP_BACKUP}"
  fi
  for kv in 'FONTFACE="Terminus"' 'FONTSIZE="32x16"'; do
    key=${kv%%=*}
    if grep -q "^${key}=" "${CONSOLE_SETUP}"; then
      sed -i "s/^${key}=.*/${kv}/" "${CONSOLE_SETUP}"
    else
      echo "${kv}" >> "${CONSOLE_SETUP}"
    fi
  done
  # --save caches the font console-setup applies at boot; --force because
  # this script's stdin is an SSH session, not a VT, and setupcon otherwise
  # declines to touch the consoles.
  if command -v setupcon >/dev/null 2>&1; then
    setupcon --save --force || echo "warning: setupcon failed; the console font is unchanged until console-setup next runs." >&2
  else
    echo "warning: setupcon not found; ${CONSOLE_SETUP} was updated but the font is unchanged until console-setup runs." >&2
  fi
else
  echo "warning: ${CONSOLE_SETUP} not found (not console-setup?); console font left as is." >&2
fi
if command -v update-grub >/dev/null 2>&1; then
  mkdir -p "$(dirname "${GRUB_DROPIN}")"
  grub_dropin_new=$(mktemp)
  cat > "${grub_dropin_new}" <<EOF_GRUB
# Harbor Console (ADR 20): ${CONSOLE_VIDEO_MODE} with a 32x16 console font gives 3x
# glyphs on a 1080p monitor. Removed by deploy/uninstall.sh.
GRUB_CMDLINE_LINUX_DEFAULT="\${GRUB_CMDLINE_LINUX_DEFAULT} video=${CONSOLE_VIDEO_MODE}"
EOF_GRUB
  # Regenerate grub.cfg only when the drop-in is new or changed.
  if ! cmp -s "${grub_dropin_new}" "${GRUB_DROPIN}"; then
    install -m 0644 "${grub_dropin_new}" "${GRUB_DROPIN}"
    update-grub
  fi
  rm -f "${grub_dropin_new}"
else
  echo "warning: update-grub not found; add 'video=${CONSOLE_VIDEO_MODE}' to the kernel command line by hand for 3x text (ADR 20)." >&2
fi
if ! grep -qw "video=${CONSOLE_VIDEO_MODE}" /proc/cmdline; then
  console_reboot_needed=1
fi
```

Note the heredoc: `\${GRUB_CMDLINE_LINUX_DEFAULT}` is escaped so the drop-in contains the literal `${GRUB_CMDLINE_LINUX_DEFAULT}` for `grub-mkconfig` to expand, while `${CONSOLE_VIDEO_MODE}` is expanded by this script. The resulting file must read exactly:

```sh
# Harbor Console (ADR 20): 1280x720 with a 32x16 console font gives 3x
# glyphs on a 1080p monitor. Removed by deploy/uninstall.sh.
GRUB_CMDLINE_LINUX_DEFAULT="${GRUB_CMDLINE_LINUX_DEFAULT} video=1280x720"
```

- [ ] **Step 3: Add the reboot notice to the final message**

Replace:

```bash
echo "Admin logins remain on tty2-tty6 (Ctrl+Alt+F2 ... F6) and via SSH."
echo
```

with:

```bash
echo "Admin logins remain on tty2-tty6 (Ctrl+Alt+F2 ... F6) and via SSH."
if [[ ${console_reboot_needed} -eq 1 ]]; then
  echo "Reboot to switch the console to ${CONSOLE_VIDEO_MODE}; until then the text is 2x, not 3x."
fi
echo
```

- [ ] **Step 4: Order the unit after console-setup**

In `deploy/harbor-console.service` change:

```ini
After=systemd-user-sessions.service
```

to:

```ini
# console-setup.service sets the 32x16 font (ADR 20) and is otherwise
# unordered against this unit; without this the first frame after boot can
# render at the 8x16 geometry and snap a second later.
After=systemd-user-sessions.service console-setup.service
```

- [ ] **Step 5: Syntax-check and exercise the editing logic on a fixture**

Run: `bash -n deploy/install.sh`
Expected: no output, exit 0.

Then, in the session scratchpad directory, exercise the console-setup edit and the heredoc on a copy of Ubuntu's default file (this is the same code as Step 2 with the paths pointed at temp files). Save this as `fixture.sh` in the scratchpad and run `bash fixture.sh` from there:

```bash
printf 'ACTIVE_CONSOLES="/dev/tty[1-6]"\nCHARMAP="UTF-8"\nCODESET="guess"\nFONTFACE="Fixed"\nFONTSIZE="8x16"\nVIDEOMODE=\n' > console-setup
CONSOLE_SETUP=./console-setup
for pass in 1 2; do
  for kv in 'FONTFACE="Terminus"' 'FONTSIZE="32x16"'; do
    key=${kv%%=*}
    if grep -q "^${key}=" "${CONSOLE_SETUP}"; then sed -i "s/^${key}=.*/${kv}/" "${CONSOLE_SETUP}"; else echo "${kv}" >> "${CONSOLE_SETUP}"; fi
  done
  echo "--- pass ${pass}"; cat console-setup
done
CONSOLE_VIDEO_MODE=1280x720
cat <<EOF_GRUB
# Harbor Console (ADR 20): ${CONSOLE_VIDEO_MODE} with a 32x16 console font gives 3x
# glyphs on a 1080p monitor. Removed by deploy/uninstall.sh.
GRUB_CMDLINE_LINUX_DEFAULT="\${GRUB_CMDLINE_LINUX_DEFAULT} video=${CONSOLE_VIDEO_MODE}"
EOF_GRUB
```

Expected: both passes show `FONTFACE="Terminus"` and `FONTSIZE="32x16"` with the other four lines unchanged, six lines total, no duplicate keys (pass 2 identical to pass 1, proving idempotence); the heredoc prints the three-line drop-in with a literal `${GRUB_CMDLINE_LINUX_DEFAULT}` and `video=1280x720`.

- [ ] **Step 6: Commit**

```bash
git add deploy/install.sh deploy/harbor-console.service
git commit -m "deploy: size the console for 3x text (font 32x16, video=1280x720)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: uninstall.sh hands the console back

**Files:**
- Modify: `deploy/uninstall.sh:45-47` (after the getty restore)

**Interfaces:**
- Consumes: `/etc/default/grub.d/harbor-console.cfg` and `/etc/default/console-setup.pre-harbor` as written by Task 1.

- [ ] **Step 1: Add the reversal after the login prompt is restored**

Directly after:

```bash
echo "==> Restoring the login prompt on tty1"
systemctl unmask getty@tty1.service 2>/dev/null || true
systemctl start getty@tty1.service 2>/dev/null || true
```

insert:

```bash
# ADR 20 reversal. The running kernel keeps the 1280x720 mode until reboot;
# the font goes back at once.
echo "==> Restoring the console font and mode (ADR 20)"
CONSOLE_SETUP=/etc/default/console-setup
CONSOLE_SETUP_BACKUP=/etc/default/console-setup.pre-harbor
GRUB_DROPIN=/etc/default/grub.d/harbor-console.cfg
if [[ -f "${GRUB_DROPIN}" ]]; then
  rm -f "${GRUB_DROPIN}"
  if command -v update-grub >/dev/null 2>&1; then
    update-grub || echo "warning: update-grub failed; run it by hand to drop video=1280x720 from the kernel command line." >&2
  else
    echo "warning: update-grub not found; remove 'video=1280x720' from the kernel command line by hand." >&2
  fi
  echo "The console returns to its native mode at the next reboot."
fi
if [[ -f "${CONSOLE_SETUP_BACKUP}" ]]; then
  mv -f "${CONSOLE_SETUP_BACKUP}" "${CONSOLE_SETUP}"
  if command -v setupcon >/dev/null 2>&1; then
    setupcon --save --force || echo "warning: setupcon failed; the font is restored at the next boot." >&2
  fi
else
  echo "warning: ${CONSOLE_SETUP_BACKUP} not found; leaving ${CONSOLE_SETUP} as it is rather than guessing what the host had." >&2
fi
```

- [ ] **Step 2: Syntax-check**

Run: `bash -n deploy/uninstall.sh`
Expected: no output, exit 0.

- [ ] **Step 3: Commit**

```bash
git add deploy/uninstall.sh
git commit -m "deploy: uninstall restores the console font and mode

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: ADR 20 and the docs that point at it

**Files:**
- Create: `docs/adr/0020-size-the-console-for-the-room.md`
- Modify: `docs/adr/README.md` (table, after the 0019 row)
- Modify: `docs/superpowers/specs/2026-09-26-console-text-size-design.md:4` (status line)
- Modify: `CLAUDE.md` (the `harbor-console` bullet under "What this is")

- [ ] **Step 1: Write the ADR**

`docs/adr/0020-size-the-console-for-the-room.md`:

```markdown
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
a 32x16 glyph covers 24x48 physical pixels -- 3x in both axes -- and the
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
- 22 rows leave 3 spare over the current dashboard. The dashboard grows one
  row per local filesystem, remote mount, volume group with slack, stray
  device and GPU; past 22 rows `rich` crops the bottom of the panel with no
  message. That is visible on the monitor, and the remedy is a smaller
  font in `install.sh` (Terminus 28x14 gives 91x25 cells, 2.6x), not
  application code that compresses rows.
- A monitor that does not advertise 1280x720 makes DRM ignore the option
  and keep its preferred mode: the console stays readable at 2x rather
  than going blank.
- `install.sh` now edits a file outside `/opt` and `/etc/systemd` that it
  did not create. The once-only backup and the restore in `uninstall.sh`
  are what keep that reversible.
- Rejected: a manual one-time host change (lost on a rebuilt host); a
  kernel-only `fbcon=font:TER16x32` (console-setup resets the font at
  boot, so it would have to be disabled); any `rich` layout change
  (cannot change glyph size).
```

- [ ] **Step 2: Add the README row**

After the 0019 row in `docs/adr/README.md` add:

```markdown
| 0020 | [Size the console for the room: Terminus 32x16 at 1280x720](0020-size-the-console-for-the-room.md) | Accepted |
```

- [ ] **Step 3: Mark the spec implemented and note it in CLAUDE.md**

In the spec, change `Status: proposed` to `Status: implemented 2026-09-26`.

In `CLAUDE.md`, in the `harbor-console` bullet under "What this is", after `Refreshes once per second, exits cleanly on Ctrl+C.` append (same paragraph):

```
`install.sh` also sizes the console it takes over: Terminus 32x16 and a 1280x720 framebuffer mode, 3x text on a 1080p monitor, 80x22 cells ([ADR 20](docs/adr/0020-size-the-console-for-the-room.md)); the dashboard has 3 rows of headroom there before `rich` crops it.
```

- [ ] **Step 4: Check links and commit**

Run: `ls docs/adr/0020-size-the-console-for-the-room.md` and `grep -c 0020 docs/adr/README.md CLAUDE.md`
Expected: the file exists; each grep count is 1.

```bash
git add docs/adr/0020-size-the-console-for-the-room.md docs/adr/README.md docs/superpowers/specs/2026-09-26-console-text-size-design.md CLAUDE.md
git commit -m "docs: ADR 20, size the console for the room

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```
