"""Finding the projects that participate in port assignment.

harbor-console lives inside the tree it scans, so the root needs no
configuration. Participation is opt-in: a project joins by adding
`.harbor.toml`, which is what keeps worktrees, archives and vendored checkouts
out without an exclusion list to maintain.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

DECLARATION_NAME = ".harbor.toml"


def tree_root(env: Mapping[str, str] | None = None, start: Path | None = None) -> Path:
    """The directory whose children are scanned for declarations."""
    environ = os.environ if env is None else env
    override = environ.get("HARBOR_TREE_ROOT")
    if override:
        return Path(override)

    repo = start if start is not None else Path(__file__).resolve().parents[3]
    return repo.parent


def find_declarations(root: Path) -> list[Path]:
    """Every direct child project's declaration, sorted by directory name."""
    if not root.is_dir():
        return []

    found = [
        child / DECLARATION_NAME
        for child in sorted(root.iterdir(), key=lambda path: path.name)
        if child.is_dir() and (child / DECLARATION_NAME).is_file()
    ]
    return found
