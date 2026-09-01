from pathlib import Path

import pytest

from harbor_console.ports.explainer import TEMPLATE_VERSION, write_explainer


def test_writes_when_missing(tmp_path: Path):
    path = tmp_path / "HARBOR_PORTS.md"

    assert write_explainer(path) is True
    assert f"harbor-console-template-version: {TEMPLATE_VERSION}" in path.read_text(
        encoding="utf-8"
    )


def test_does_not_rewrite_a_current_file(tmp_path: Path):
    path = tmp_path / "HARBOR_PORTS.md"
    write_explainer(path)
    path.write_text(
        path.read_text(encoding="utf-8") + "\nlocal note\n", encoding="utf-8"
    )

    assert write_explainer(path) is False
    assert "local note" in path.read_text(encoding="utf-8")


def test_rewrites_an_older_version(tmp_path: Path):
    path = tmp_path / "HARBOR_PORTS.md"
    path.write_text("harbor-console-template-version: 0\nstale\n", encoding="utf-8")

    assert write_explainer(path) is True
    assert "stale" not in path.read_text(encoding="utf-8")


def test_contains_no_project_specific_numbers(tmp_path: Path):
    path = tmp_path / "HARBOR_PORTS.md"
    write_explainer(path)
    text = path.read_text(encoding="utf-8")

    assert "8090" not in text
    assert "8100-8999" in text
    assert "hcstatus" in text


def _truncating_disk_full(monkeypatch) -> None:
    """Make every `Path.write_text` empty its target and then fail.

    Mirrors the helper duplicated in the envfile/declaration/ledger tests -- a
    real out-of-space write truncates its target at open, before a single byte
    is stored.
    """

    def write_text(self: Path, data: str, encoding: str | None = None, **kwargs) -> int:
        with open(self, "w", encoding="utf-8"):
            pass
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", write_text)


def test_a_failed_write_leaves_the_existing_explainer_intact(tmp_path: Path, monkeypatch):
    # The explainer is now the first file written for a project. A plain
    # truncating write that fails partway can leave a file that still passes
    # the version check above -- a permanently truncated explainer that no
    # later run would ever repair, unlike every other truncation shape here.
    # Content is a stale (pre-rewrite) version so the write actually runs.
    path = tmp_path / "HARBOR_PORTS.md"
    original = "harbor-console-template-version: 0\nstale\n"
    path.write_text(original, encoding="utf-8")
    _truncating_disk_full(monkeypatch)

    with pytest.raises(OSError):
        write_explainer(path)

    assert path.read_text(encoding="utf-8") == original
    assert [entry.name for entry in tmp_path.iterdir()] == ["HARBOR_PORTS.md"]
