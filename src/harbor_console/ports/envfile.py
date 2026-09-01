"""The managed fence in a project's `.env`.

`.env` usually holds secrets and is usually gitignored, so this writer only ever
rewrites the lines between its own markers. Everything outside them is preserved
exactly.
"""

from __future__ import annotations

from pathlib import Path

FENCE_START = "# >>> harbor-console (managed) >>>"
FENCE_END = "# <<< harbor-console (managed) <<<"


def apply_fence(text: str, values: dict[str, str]) -> str:
    """Return `text` with the managed block replaced by `values`."""
    body = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    block = f"{FENCE_START}\n{body}\n{FENCE_END}\n"

    if FENCE_START in text and FENCE_END in text:
        head, _, rest = text.partition(FENCE_START)
        _, _, tail = rest.partition(FENCE_END)
        return head + block + tail.lstrip("\n")

    if not text:
        return block
    if not text.endswith("\n"):
        text += "\n"
    return text + block


def write_env(path: Path, values: dict[str, str]) -> None:
    """Create or update a project's `.env`, preserving everything else in it."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(apply_fence(existing, values), encoding="utf-8")
