"""Whole-file writes that cannot destroy the file they are replacing.

Every writer in the allocator rewrites a file *this tool does not own*: another
repository's `.env` (secrets, usually gitignored, never backed up), its
`.harbor.toml`, and this tool's own ledger. `Path.write_text` truncates the
target at open, so a write that fails partway -- a full disk, a revoked
permission, an I/O error -- leaves somebody else's secrets replaced by nothing.

So the new content is written to a temporary file *in the same directory* as the
target, and only then moved over it with `os.replace`, which is atomic on POSIX
and on Windows. A reader therefore sees either the whole old file or the whole
new one, and any failure before the replace leaves the original exactly as it
was. Same directory matters: `os.replace` cannot be atomic -- and on Windows
cannot work at all -- across filesystems, and the system temporary directory is
routinely on a different one.

The `except BaseException` cleanup below handles every failure that runs as a
Python exception, including a `KeyboardInterrupt` or `SystemExit` raised mid-write.
It cannot run at all for a `SIGKILL` or a power cut: those leave the temp file
sitting in the target's own directory, next to secrets it may itself contain
in full or in part. To keep that residue from ever becoming a *tracked* file
-- a `.gitignore` entry for `.env` does not match `.env.<random>.tmp` -- every
temp file this module creates is named
`.harbor-tmp.<original name>.<random>.tmp`, so a single `.gitignore` rule,
`.harbor-tmp.*`, sweeps every leftover
regardless of which target was being written. That pattern is the sanctioned
way to find and remove abandoned temp files; nothing in this module sweeps
them automatically.

stdlib only: `tempfile` and `os`.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def write_text_atomic(path: Path, text: str) -> None:
    """Replace `path`'s contents with `text`, or leave the file untouched.

    Writes UTF-8. If the write or the replace fails, the temporary file is
    removed and the original file is left exactly as it was; the underlying
    exception propagates unchanged, so callers keep the error handling they
    already have.

    An existing file keeps its permission bits, which matters for a `.env` that
    the owning project may have deliberately locked down. A file being created
    for the first time gets the mode an ordinary write would have given it
    (`0o666` less the process umask) rather than the private mode `mkstemp`
    hands out.
    """
    mode = _target_mode(path)

    handle, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".harbor-tmp.{path.name}.", suffix=".tmp"
    )
    os.close(handle)
    temp_path = Path(temp_name)

    try:
        temp_path.write_text(text, encoding="utf-8")
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _target_mode(path: Path) -> int:
    """The permission bits the replacement file should end up with.

    An existing target's own bits are preserved. For a new file the mode an
    ordinary create would produce is reconstructed from the process umask, read
    the only way the C library allows -- by setting it and setting it straight
    back.
    """
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        umask = os.umask(0o077)
        os.umask(umask)
        return 0o666 & ~umask
