import os
from pathlib import Path

import pytest

from harbor_console.ports.envfile import (
    EnvFenceError,
    FENCE_END,
    FENCE_START,
    apply_fence,
    write_env,
)


def test_creates_the_fence_when_absent():
    result = apply_fence("DB_PASSWORD=hunter2\n", {"HARBOR_PORT_WEB": "8090"})

    assert "DB_PASSWORD=hunter2" in result
    assert FENCE_START in result
    assert "HARBOR_PORT_WEB=8090" in result
    assert FENCE_END in result


def test_replaces_only_the_fence_contents():
    original = (
        "BEFORE=1\n"
        f"{FENCE_START}\n"
        "HARBOR_PORT_WEB=8080\n"
        f"{FENCE_END}\n"
        "AFTER=2\n"
    )

    result = apply_fence(original, {"HARBOR_PORT_WEB": "8090"})

    assert "BEFORE=1" in result
    assert "AFTER=2" in result
    assert "8080" not in result
    assert "HARBOR_PORT_WEB=8090" in result
    assert result.count(FENCE_START) == 1


def test_secrets_outside_the_fence_survive_byte_for_byte():
    original = f"A=1\n{FENCE_START}\nHARBOR_PORT_WEB=1\n{FENCE_END}\n# trailing comment\n"

    result = apply_fence(original, {"HARBOR_PORT_WEB": "2"})

    assert result.startswith("A=1\n")
    assert result.endswith("# trailing comment\n")


def test_empty_input_produces_just_the_fence():
    result = apply_fence("", {"HARBOR_PORT_WEB": "8090"})

    assert result == f"{FENCE_START}\nHARBOR_PORT_WEB=8090\n{FENCE_END}\n"


def test_write_env_creates_a_missing_file(tmp_path: Path):
    path = tmp_path / ".env"

    write_env(path, {"HARBOR_PORT_WEB": "8090"})

    assert "HARBOR_PORT_WEB=8090" in path.read_text(encoding="utf-8")


def test_write_env_is_idempotent(tmp_path: Path):
    path = tmp_path / ".env"
    write_env(path, {"HARBOR_PORT_WEB": "8090"})
    first = path.read_text(encoding="utf-8")

    write_env(path, {"HARBOR_PORT_WEB": "8090"})

    assert path.read_text(encoding="utf-8") == first


def test_orphan_end_before_start_raises_instead_of_discarding_content():
    original = f"A=1\n{FENCE_END}\nB=2\n{FENCE_START}\nC=3\n"

    with pytest.raises(EnvFenceError):
        apply_fence(original, {"HARBOR_PORT_WEB": "1"})


def test_orphan_start_with_no_end_anywhere_raises_instead_of_duplicating():
    original = f"A=1\n{FENCE_START}\nB=2\n"

    with pytest.raises(EnvFenceError):
        apply_fence(original, {"HARBOR_PORT_WEB": "1"})


def test_intentional_blank_line_after_fence_survives_a_rewrite():
    original = f"{FENCE_START}\nHARBOR_PORT_WEB=1\n{FENCE_END}\n\n# spaced comment\n"

    result = apply_fence(original, {"HARBOR_PORT_WEB": "2"})

    assert result.endswith(f"{FENCE_END}\n\n# spaced comment\n")


def _truncating_disk_full(monkeypatch) -> None:
    """Make every `Path.write_text` empty its target and then fail.

    This is what a real out-of-space write does: `write_text` opens the file for
    writing -- which truncates it -- before a single byte is stored. A writer
    that writes straight to its target therefore destroys the file it was
    updating; one that writes to a temporary file beside it destroys only that.
    """

    def write_text(self: Path, data: str, encoding: str | None = None, **kwargs) -> int:
        with open(self, "w", encoding="utf-8"):
            pass
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", write_text)


def test_a_failed_write_leaves_the_existing_env_intact(tmp_path: Path, monkeypatch):
    # `.env` holds somebody else's secrets, is usually gitignored and is never
    # backed up. A write that fails must leave the file it found, not an empty
    # one -- there is nothing to restore it from.
    path = tmp_path / ".env"
    original = "SECRET=hunter2\nAPI_KEY=abcdef\n"
    path.write_text(original, encoding="utf-8")
    _truncating_disk_full(monkeypatch)

    with pytest.raises(OSError):
        write_env(path, {"HARBOR_PORT_WEB": "8090"})

    assert path.read_text(encoding="utf-8") == original
    assert [entry.name for entry in tmp_path.iterdir()] == [".env"]


def test_write_env_leaves_an_already_correct_file_alone(tmp_path: Path):
    # A project is put into the write pass by *any* of its files having drifted,
    # so an unconditional rewrite here moves the mtime of every participating
    # project's `.env` whenever the explainer template changes. Identical
    # content is invisible; a moved mtime is not.
    path = tmp_path / ".env"
    write_env(path, {"HARBOR_PORT_WEB": "8090"})
    os.utime(path, (1_000_000, 1_000_000))
    stamp = path.stat().st_mtime_ns

    assert write_env(path, {"HARBOR_PORT_WEB": "8090"}) is False
    assert path.stat().st_mtime_ns == stamp
    assert write_env(path, {"HARBOR_PORT_WEB": "8091"}) is True
