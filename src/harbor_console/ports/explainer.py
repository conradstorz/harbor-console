"""`HARBOR_PORTS.md`: the rules, dropped into every participating project.

Identical everywhere and free of project-specific numbers, so it can never go
stale and never needs regenerating when an assignment changes. It exists so that
a human -- or an agent -- working inside another repository can find out why a
port is fenced into `.env` without ever seeing harbor-console.
"""

from __future__ import annotations

import re
from pathlib import Path

from harbor_console.ports.atomic import write_text_atomic

TEMPLATE_VERSION = 2

_VERSION_LINE = re.compile(r"harbor-console-template-version:\s*(\d+)")

TEMPLATE = f"""\
# Harbor Console — port assignment

harbor-console-template-version: {TEMPLATE_VERSION}

This file is placed in every project that participates in port assignment. It is
identical everywhere and contains no numbers. It is written by harbor-console;
edits are overwritten when the template version changes.

## Why this exists

Published host ports are assigned centrally, not chosen per project. Two projects
once claimed the same port. The loser bound nothing, logged it, and kept running
— so the only symptom was a dashboard that never appeared on a service that
otherwise looked healthy. Nothing decided who owned the port, and nothing checked
before the second project claimed it.

## The three files

| File | Holds | Owned by |
| --- | --- | --- |
| `.harbor.toml` (this project) | what this project **wants** | you |
| `.env` (this project) | what it **got** — the effective number | harbor-console |
| `services.toml` (harbor-console) | the **lease** — who holds what, since when | harbor-console |

`.harbor.toml` also carries an `assigned` field. `want` is yours and is never
rewritten; `assigned` is harbor-console's and must not be hand-edited.

## Rules

- **Do not hard-code a published port in compose.** Use the variable with a
  default: `"${{HARBOR_PORT_NAME:-1234}}:1234"`. The default is what lets this
  project start on a machine where harbor-console has never run.
- **Do not edit inside the `# >>> harbor-console (managed) >>>` fence** in
  `.env`. It is rewritten on every sync. Everything outside it is preserved.
- **To change a port**, edit `want` in `.harbor.toml`, then run
  `harbor-console ports sync` from the harbor-console checkout. You may not get
  what you asked for — if another project already holds it, you are moved and
  told so.
- **The incumbent always wins.** A port already leased is never taken from its
  holder, and a running service is never renumbered underneath you.
- **A stopped service keeps its port.** Ports are not reclaimed because nothing
  is listening.
- **New ports come from 8100-8999**, deliberately below the Linux ephemeral range
  (32768-60999) so an assigned port cannot lose a race to an outbound socket.
- **After a reassignment, do two things: update the compose default, then
  redeploy.** They fix different problems. Redeploying is what makes the running
  container actually bind the new port. Editing the default in the compose line
  — the `1234` in `"${{HARBOR_PORT_NAME:-1234}}:1234"` — to the number you were
  assigned is what clears the warning: until you do, `harbor-console ports scan`
  and `harbor-console ports sync` both keep reporting that this project's
  compose file defaults the variable to the old number, and both keep exiting
  non-zero. That warning is permanent, not transient. Nothing else clears it,
  because `.env` is normally gitignored and the stale default is what a fresh
  clone would publish.

## Add one line to `.gitignore`

    .harbor-tmp.*

harbor-console never truncates a file it is replacing. It writes the new content
to `.harbor-tmp.<file>.<random>.tmp` beside the target and then moves it over,
so a write that fails partway leaves your original exactly as it was, and it
removes the temp file afterwards.

A `SIGKILL` or a power cut is the case it cannot clean up after. That leaves a
temp file sitting in your repository next to — and possibly containing part of —
your `.env`. A `.gitignore` rule for `.env` does not match a temp file derived
from it, so without the line above an abandoned temp file holding your secrets
can be committed.

The same pattern is how you find and delete leftovers. Nothing sweeps them
automatically.

## Health and status endpoints

harbor-console probes each declared port to show whether this project is up.

- `health_path` (usually `/`) — **any HTTP response means up**, including a
  redirect to a login page. A probe insisting on 200 would call a healthy service
  down.
- `hcstatus_path` (optional, conventionally `/hcstatus`) — richer detail,
  rendered on the status page. It never affects up/down: if it is missing,
  broken, slow, or malformed, this project still shows as up, with a warning.
  Return:

      {{"state": "ok",
        "summary": "3 queued",
        "detail": [{{"label": "queue", "value": "3"}},
                   {{"label": "last run", "value": "14:02"}}]}}

  `state` is `ok`, `warn`, or `error`. `summary` is one short line. `detail` is a
  list of label/value pairs of your choosing, rendered verbatim.

## If harbor-console is gone

Nothing here breaks. The compose default keeps the project running, the numbers
in `.env` and `.harbor.toml` stay valid, and this file explains the convention
well enough to keep following it by hand.
"""


def is_outdated(path: Path) -> bool:
    """True when `path` is missing, unreadable, or older than the template.

    Split out of `write_explainer` so that a caller can ask the question without
    answering it -- `scan` has to report a missing explainer while writing
    nothing at all. A file that cannot be read is treated as outdated: the write
    that follows is the thing entitled to fail, not the question.
    """
    if not path.exists():
        return True
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return True
    match = _VERSION_LINE.search(text)
    return match is None or int(match.group(1)) < TEMPLATE_VERSION


def write_explainer(path: Path) -> bool:
    """Write the explainer when missing or outdated. Returns True when written.

    Written atomically: this is the first file `_write_project` touches for a
    project, so a plain truncating write that failed partway could leave a file
    that still satisfies the version check above -- a permanently truncated
    explainer that no later run would ever repair. `write_text_atomic` leaves
    the original untouched on any failure instead.
    """
    if not is_outdated(path):
        return False

    write_text_atomic(path, TEMPLATE)
    return True
