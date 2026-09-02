"""The managed fence in a project's `.env`.

`.env` usually holds secrets and is usually gitignored, so this writer only ever
rewrites the lines between its own markers. Everything outside them is preserved
exactly.
"""

from __future__ import annotations

from pathlib import Path

from harbor_console.ports.atomic import write_text_atomic

FENCE_START = "# >>> harbor-console (managed) >>>"
FENCE_END = "# <<< harbor-console (managed) <<<"


class EnvFenceError(Exception):
    """Raised when the managed fence markers in a `.env` file are corrupted.

    This is a new exception type: any caller of `write_env` or `apply_fence`
    must be prepared to catch it. Guessing how to repair a malformed fence
    risks destroying secrets that live outside it, so this module refuses to
    guess and raises instead.
    """


def fenced_values(text: str) -> dict[str, str]:
    """The variables the managed block of `text` currently publishes.

    The read half of `apply_fence`, and the answer to "is this one variable
    already published as the ledger says?" -- which is a different question
    from `apply_fence`'s "would writing the whole fence change this file?", and
    the one a caller asks when part of the fence is about to be rewritten
    anyway. Nothing outside the markers is looked at: a `HARBOR_PORT_WEB` a
    project set for itself elsewhere in its `.env` is not something this tool
    published, and counting it would leave the managed block unrepaired.

    Never raises. A file with no fence, or with corrupted markers, publishes
    nothing as far as this function is concerned; refusing a corrupted fence is
    `apply_fence`'s job, and every caller here reaches it too.
    """
    start = text.find(FENCE_START)
    end = text.find(FENCE_END)
    if start == -1 or end == -1 or start > end:
        return {}

    values: dict[str, str] = {}
    for line in text[start + len(FENCE_START) : end].splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


def apply_fence(text: str, values: dict[str, str]) -> str:
    """Return `text` with the managed block replaced by `values`.

    A well-formed fence requires a `FENCE_START` followed, later in the text,
    by a `FENCE_END`; that is the only arrangement treated as "replace this
    block". Any other arrangement of markers -- a start with no end after it,
    an end with no start before it -- means the fence is corrupted, and this
    function raises `EnvFenceError` rather than guess at a repair: guessing
    wrong can silently discard secrets that live outside the fence. A file
    with no markers at all is not corrupted; that is the ordinary case of a
    project adopting the fence for the first time, and the block is appended.
    """
    body = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    block = f"{FENCE_START}\n{body}\n{FENCE_END}\n"

    start_index = text.find(FENCE_START)
    end_index = text.find(FENCE_END)

    if start_index != -1 and end_index != -1 and start_index < end_index:
        head, _, rest = text.partition(FENCE_START)
        _, _, tail = rest.partition(FENCE_END)
        if tail.startswith("\n"):
            tail = tail[1:]
        return head + block + tail

    if start_index != -1 or end_index != -1:
        if start_index != -1 and end_index == -1:
            raise EnvFenceError(
                f"found {FENCE_START!r} with no matching {FENCE_END!r} after it"
            )
        if end_index != -1 and start_index == -1:
            raise EnvFenceError(
                f"found {FENCE_END!r} with no matching {FENCE_START!r} before it"
            )
        raise EnvFenceError(
            f"found {FENCE_END!r} before {FENCE_START!r}; the fence markers are out of order"
        )

    if not text:
        return block
    if not text.endswith("\n"):
        text += "\n"
    return text + block


def write_env(path: Path, values: dict[str, str]) -> bool:
    """Create or update a project's `.env`, preserving everything else in it.

    Returns True when the file was written. A `.env` that already says exactly
    this is left alone -- not opened, not replaced, mtime and all -- for the
    same reason `explainer.write_explainer` checks before writing: a project is
    put into the write pass by *any* of its files having drifted, so a run that
    rewrote every file of every drifted project would touch the `.env` of every
    participating repository each time the explainer template changed. A
    byte-identical rewrite is invisible in content and loud in `git status`,
    file watchers and backups, in repositories this tool does not own.

    Written atomically: `.env` holds secrets that are not this tool's to lose,
    so a failed write must leave the file it found, not an empty one.
    """
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    wanted = apply_fence(existing, values)
    if path.exists() and wanted == existing:
        return False

    write_text_atomic(path, wanted)
    return True
