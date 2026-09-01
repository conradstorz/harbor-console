"""Reading published ports out of a project's compose files.

Regex rather than a YAML parser: this project takes no new runtime dependency,
and the only thing needed is the published-port strings. Used to warn when a
compose default has drifted from the assignment -- `.env` is usually gitignored,
so the default is what a fresh clone actually gets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_VARIABLE = re.compile(r'"\$\{(?P<var>[A-Z0-9_]+)(?::-(?P<default>\d+))?\}:\d+"')
_LITERAL = re.compile(r'"(?:(?P<addr>[\d.]+):)?(?P<host_port>\d+):\d+"')


@dataclass(frozen=True)
class PublishedPort:
    """One published-port entry found in a compose file."""

    file: Path
    var: str | None
    default: int | None
    literal: int | None


def published_ports(project_dir: Path) -> list[PublishedPort]:
    """Every published port declared by every compose variant in a project."""
    found: list[PublishedPort] = []

    for path in sorted(project_dir.glob("docker-compose*.y*ml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("-"):
                continue

            match = _VARIABLE.search(stripped)
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

            match = _LITERAL.search(stripped)
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
