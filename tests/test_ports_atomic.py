"""Direct unit tests for `write_text_atomic`.

`atomic.write_text_atomic` is otherwise exercised only through other modules'
failure-path tests (`test_ports_envfile.py`, `test_ports_declaration.py`,
`test_ports_ledger.py`), so its success path, its create-a-new-file path, its
temp-file naming, and its permission-bit handling are pinned by nothing on
their own. These tests use only `tmp_path` -- never the real project tree,
since this module rewrites files this tool does not own.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from harbor_console.ports.atomic import write_text_atomic


def _residue(directory: Path, *, exclude: str) -> list[str]:
    """Directory entries other than `exclude` -- i.e. any leftover temp file."""
    return [entry.name for entry in directory.iterdir() if entry.name != exclude]


def test_replaces_an_existing_file_with_no_residue(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("old\n", encoding="utf-8")

    write_text_atomic(path, "new\n")

    assert path.read_text(encoding="utf-8") == "new\n"
    assert _residue(tmp_path, exclude=".env") == []


def test_creates_a_missing_file_with_no_residue(tmp_path: Path) -> None:
    path = tmp_path / ".env"

    write_text_atomic(path, "new\n")

    assert path.read_text(encoding="utf-8") == "new\n"
    assert _residue(tmp_path, exclude=".env") == []


def test_temp_file_name_matches_the_sweep_pattern(tmp_path: Path, monkeypatch) -> None:
    """The name chosen for the temp file must be caught by `.harbor-tmp.*`.

    A `.gitignore` entry for `.env` does not match `.env.<random>.tmp`, so a
    temp file abandoned by a `SIGKILL` or a power cut -- anything that skips
    the `except BaseException` cleanup -- becomes trackable. This inspects the
    name chosen during an ordinary, successful write, at the point
    `os.replace` is called and before it is renamed away.
    """
    path = tmp_path / ".env"
    seen: dict[str, str] = {}
    real_replace = os.replace

    def spy_replace(src: "os.PathLike[str] | str", dst: "os.PathLike[str] | str") -> None:
        seen["name"] = Path(src).name
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)

    write_text_atomic(path, "new\n")

    assert seen["name"].startswith(".harbor-tmp.")


def _truncating_disk_full(monkeypatch) -> None:
    """Make every `Path.write_text` empty its target and then fail.

    Mirrors the helper duplicated in the envfile/declaration/ledger tests: a
    real out-of-space write truncates its target at open, before a single byte
    is stored. Patching `Path.write_text` globally reaches the temp file's own
    write inside `write_text_atomic`, since it is written the same way.
    """

    def write_text(self: Path, data: str, encoding: str | None = None, **kwargs) -> int:
        with open(self, "w", encoding="utf-8"):
            pass
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", write_text)


def test_a_failure_partway_through_leaves_the_original_intact_and_removes_temp(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / ".env"
    original = "SECRET=hunter2\n"
    path.write_text(original, encoding="utf-8")
    _truncating_disk_full(monkeypatch)

    with pytest.raises(OSError):
        write_text_atomic(path, "new\n")

    assert path.read_text(encoding="utf-8") == original
    assert _residue(tmp_path, exclude=".env") == []


def test_a_failure_when_the_target_never_existed_creates_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / ".env"
    _truncating_disk_full(monkeypatch)

    with pytest.raises(OSError):
        write_text_atomic(path, "new\n")

    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_target_mode_preserves_an_existing_files_stat_bits(monkeypatch) -> None:
    """`_target_mode` returns an existing file's own bits, not a recomputed default.

    Exercised directly against a faked `Path.stat` rather than through a real
    `os.chmod`/`os.replace` round trip: on Windows, `os.chmod` collapses every
    mode down to just two states (writable or read-only), and `os.replace`
    additionally refuses to overwrite a read-only destination at all -- so no
    mode distinguishable from the "new file" default can survive a real
    replace on this platform regardless of whether preservation works. This
    pins the preservation logic itself, independent of that OS ceiling.
    """
    from harbor_console.ports.atomic import _target_mode

    path = Path("does-not-need-to-exist/.env")
    fake_mode = stat.S_IFREG | 0o640

    def fake_stat(self: Path, *args, **kwargs):
        return os.stat_result((fake_mode, 0, 0, 0, 0, 0, 0, 0, 0, 0))

    monkeypatch.setattr(Path, "stat", fake_stat)

    assert _target_mode(path) == 0o640


def test_an_existing_files_permission_bits_are_preserved_across_a_replace(
    tmp_path: Path, monkeypatch
) -> None:
    """End-to-end: the mode applied to the temp file survives onto the target.

    The target starts read-only (0o444) -- the one mode Windows can actually
    distinguish from the "new file" default. Real `os.replace` on Windows
    refuses to overwrite a read-only destination at all (`ERROR_ACCESS_DENIED`),
    which is a limitation of that OS call, not of `write_text_atomic`'s
    chmod-then-replace sequence this test targets -- so the destination's
    read-only attribute is cleared immediately before the real replace runs,
    isolating the behaviour under test: that the temp file was chmod'd to the
    target's original mode beforehand, and that mode is what lands on disk.
    """
    path = tmp_path / ".env"
    path.write_text("old\n", encoding="utf-8")
    os.chmod(path, 0o444)
    real_replace = os.replace

    def unlock_then_replace(src: "os.PathLike[str] | str", dst: "os.PathLike[str] | str") -> None:
        if os.path.exists(dst):
            os.chmod(dst, 0o666)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", unlock_then_replace)

    try:
        write_text_atomic(path, "new\n")

        assert path.read_text(encoding="utf-8") == "new\n"
        assert stat.S_IMODE(path.stat().st_mode) == 0o444
    finally:
        os.chmod(path, 0o666)
