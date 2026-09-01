from pathlib import Path

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
