"""Reading published ports out of a project's compose files.

Regex rather than a YAML parser: this project takes no new runtime dependency,
and the only thing needed is the published-port strings. Used to warn when a
compose default has drifted from the assignment -- `.env` is usually gitignored,
so the default is what a fresh clone actually gets.

Recognised short-syntax `ports:` entry shapes, quoted or not, single- or
double-quoted, with an optional trailing `/tcp` or `/udp`:

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


def published_ports(project_dir: Path) -> list[PublishedPort]:
    """Every published port declared by every compose variant in a project."""
    found: list[PublishedPort] = []

    patterns = ("docker-compose*.y*ml", "compose*.y*ml")
    paths = sorted({path for pattern in patterns for path in project_dir.glob(pattern)})

    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("-"):
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
