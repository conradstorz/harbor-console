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
