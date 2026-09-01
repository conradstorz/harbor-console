"""Reading published ports out of a project's compose files.

Regex rather than a YAML parser: this project takes no new runtime dependency,
and the only thing needed is the published-port strings. Used to warn when a
compose default has drifted from the assignment -- `.env` is usually gitignored,
so the default is what a fresh clone actually gets.

The scan is structurally aware of the `ports:` key: only dash (`-`) list
entries that sit inside a `ports:` block -- indented deeper than the block's
`ports:` key line, per YAML's indentation rules -- are considered as
candidates. Indentation alone (no YAML parser) is used to find a block's
extent: it opens at a line that is exactly a `ports:` key (nothing but
optional whitespace or a trailing comment after the colon), and closes at the
first following non-blank, non-comment line that is *not* a dash entry and is
indented at or shallower than that key -- or at a dash entry indented
*shallower* than it, which belongs to some enclosing sequence.

A dash entry at exactly the key's own indentation stays inside the block. YAML
allows a block sequence to sit at the indentation of the key that owns it, and
compose files are commonly written that way:

    ports:
    - "8080:8080"

Closing on `indent <= ports_indent` made every such file invisible to the drift
auditor, which is the only warning a project gets when its `.env` has drifted
from its compose default.

Leading whitespace is counted character by character, so a tab counts as one
indent unit exactly as a space does, in the key line and in its entries alike.
That keeps a tab-indented file self-consistent, which is all this comparison
needs; YAML forbids tabs for indentation anyway, so no correct file mixes the
two in a way that could be measured differently.

A file may hold several such blocks (one per service); all of them
are scanned. This keeps port-shaped scalars that live under unrelated keys --
`command:`, `entrypoint:`, `healthcheck:` test arrays, environment lists,
`x-*` extension blocks -- out of consideration entirely, since a wrong port
number here would fabricate a drift report against a project that is actually
correct.

Within a `ports:` block, recognised short-syntax entry shapes, quoted or not,
single- or double-quoted, with an optional trailing `/tcp` or `/udp`:

- `host:container` (e.g. `8080:8080`)
- `addr:host:container` (e.g. `127.0.0.1:8080:8080`)
- `${VAR}:container` / `${VAR:-default}:container`

Deliberately unmatched, and left failing closed rather than guessed at: long
syntax (`- target: 80` / `published: 8080`) and port ranges
(`"8000-8005:8000-8005"`) both need real structure parsing that a line-regex
can't safely provide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Matches a line that is exactly a `ports:` mapping key -- nothing but
# optional leading indentation and an optional trailing comment after the
# colon -- so it can be told apart from `ports: []` or a `some_ports:` key,
# neither of which opens a block.
_PORTS_KEY = re.compile(r"^(?P<indent>[ \t]*)ports:[ \t]*(?:#.*)?$")

# Matched against a single list-entry value after the leading "- " and any
# single layer of wrapping quotes have already been stripped by `_value_of`.
_VARIABLE = re.compile(
    r"^\$\{(?P<var>[A-Z0-9_]+)(?::-(?P<default>\d+))?\}:\d+(?:/(?:tcp|udp))?$"
)
_LITERAL = re.compile(
    r"^(?:(?P<addr>[\d.]+):)?(?P<host_port>\d+):\d+(?:/(?:tcp|udp))?$"
)


@dataclass(frozen=True)
class PublishedPort:
    """One published-port entry found in a compose file."""

    file: Path
    var: str | None
    default: int | None
    literal: int | None


def _value_of(list_entry: str) -> str:
    """The scalar value of a `- ...` YAML list entry, one quote layer stripped.

    `list_entry` is a line already known to start with `-`. This is a small,
    deliberately narrow stand-in for YAML scalar parsing -- just enough to
    normalise `- "8080:8080"`, `- '8080:8080'` and `- 8080:8080` to the same
    string, not a general unescaper.
    """
    value = list_entry[1:].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _published_ports_in_file(path: Path) -> list[PublishedPort]:
    """Every published port inside `ports:` blocks of a single compose file.

    A file that cannot be read, or that is not valid UTF-8, yields nothing
    rather than raising. This reads a compose file in somebody else's
    repository, from a caller that only wants to warn about drift; a byte this
    module cannot decode is not a reason to fail the run, and the alternative --
    `UnicodeDecodeError` escaping as a traceback -- is exactly what the `.env`
    reader was already fixed for. Missing a warning is the safe direction:
    nothing is written on the strength of one.
    """
    found: list[PublishedPort] = []
    ports_indent: int | None = None

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            # Blank and comment-only lines never open or close a block.
            continue

        indent = len(line) - len(line.lstrip())

        key_match = _PORTS_KEY.fullmatch(line.rstrip())
        if key_match is not None:
            ports_indent = len(key_match.group("indent"))
            continue

        is_entry = stripped.startswith("-")

        if ports_indent is not None:
            if not is_entry and indent <= ports_indent:
                # A sibling (or shallower) key ends the block.
                ports_indent = None
            elif is_entry and indent < ports_indent:
                # A dash belonging to an enclosing sequence, not to this key.
                ports_indent = None

        if ports_indent is None or not is_entry:
            continue

        value = _value_of(stripped)

        match = _VARIABLE.fullmatch(value)
        if match is not None:
            default = match.group("default")
            found.append(
                PublishedPort(
                    file=path,
                    var=match.group("var"),
                    default=int(default) if default else None,
                    literal=None,
                )
            )
            continue

        match = _LITERAL.fullmatch(value)
        if match is not None:
            found.append(
                PublishedPort(
                    file=path,
                    var=None,
                    default=None,
                    literal=int(match.group("host_port")),
                )
            )

    return found


def published_ports(project_dir: Path) -> list[PublishedPort]:
    """Every published port declared by every compose variant in a project."""
    patterns = ("docker-compose*.y*ml", "compose.y*ml")
    paths = sorted({path for pattern in patterns for path in project_dir.glob(pattern)})

    found: list[PublishedPort] = []
    for path in paths:
        found.extend(_published_ports_in_file(path))
    return found
