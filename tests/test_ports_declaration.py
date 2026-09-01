from pathlib import Path

import pytest

from harbor_console.ports.declaration import (
    DeclarationError,
    load_declaration,
    write_assigned,
)

FULL = """\
project = "imageharbor"
host    = "hpz440"

[[port]]
name          = "dashboard"   # the web UI
want          = 8080
container     = "imageharbor"
health_path   = "/"
hcstatus_path = "/hcstatus"
description   = "Photo organiser dashboard"
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".harbor.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_reads_every_field(tmp_path: Path):
    decl = load_declaration(_write(tmp_path, FULL))

    assert decl.project == "imageharbor"
    assert decl.host == "hpz440"
    assert len(decl.ports) == 1
    port = decl.ports[0]
    assert port.name == "dashboard"
    assert port.want == 8080
    assert port.assigned is None
    assert port.addr == "0.0.0.0"
    assert port.container == "imageharbor"
    assert port.health_path == "/"
    assert port.hcstatus_path == "/hcstatus"


def test_optional_fields_default(tmp_path: Path):
    decl = load_declaration(
        _write(tmp_path, 'project = "p"\nhost = "h"\n\n[[port]]\nname = "web"\n')
    )
    port = decl.ports[0]

    assert port.want is None
    assert port.assigned is None
    assert port.addr == "0.0.0.0"
    assert port.container is None
    assert port.health_path == "/"
    assert port.hcstatus_path is None
    assert port.description == ""


def test_declaration_with_no_ports_is_valid(tmp_path: Path):
    decl = load_declaration(_write(tmp_path, 'project = "shared-postgres"\nhost = "hpz440"\n'))

    assert decl.ports == ()


def test_missing_project_is_an_error(tmp_path: Path):
    with pytest.raises(DeclarationError, match="project"):
        load_declaration(_write(tmp_path, 'host = "hpz440"\n'))


def test_duplicate_port_name_is_an_error(tmp_path: Path):
    text = 'project = "p"\nhost = "h"\n\n[[port]]\nname = "web"\n\n[[port]]\nname = "web"\n'
    with pytest.raises(DeclarationError, match="web"):
        load_declaration(_write(tmp_path, text))


def test_write_assigned_adds_the_field_and_keeps_comments(tmp_path: Path):
    path = _write(tmp_path, FULL)

    write_assigned(path, "dashboard", 8090)

    text = path.read_text(encoding="utf-8")
    assert "assigned      = 8090" in text
    assert "# the web UI" in text
    assert "want          = 8080" in text
    assert load_declaration(path).ports[0].assigned == 8090


def test_write_assigned_replaces_an_existing_value(tmp_path: Path):
    path = _write(tmp_path, FULL)
    write_assigned(path, "dashboard", 8090)
    write_assigned(path, "dashboard", 8091)

    text = path.read_text(encoding="utf-8")
    assert text.count("assigned") == 1
    assert load_declaration(path).ports[0].assigned == 8091


def test_write_assigned_targets_the_right_port_block(tmp_path: Path):
    text = (
        'project = "p"\nhost = "h"\n\n[[port]]\nname = "a"\nwant = 1\n'
        '\n[[port]]\nname = "b"\nwant = 2\n'
    )
    path = _write(tmp_path, text)

    write_assigned(path, "b", 8100)

    ports = {p.name: p.assigned for p in load_declaration(path).ports}
    assert ports == {"a": None, "b": 8100}


def test_write_assigned_rejects_an_unknown_port_name(tmp_path: Path):
    path = _write(tmp_path, FULL)

    with pytest.raises(DeclarationError, match="nosuch"):
        write_assigned(path, "nosuch", 8100)


def test_write_assigned_handles_missing_trailing_newline(tmp_path: Path):
    text = 'project = "p"\nhost = "h"\n\n[[port]]\nname = "a"'
    path = _write(tmp_path, text)

    write_assigned(path, "a", 8100)

    text = path.read_text(encoding="utf-8")
    assert 'name = "a"\n' in text
    assert load_declaration(path).ports[0].assigned == 8100


def test_write_assigned_does_not_bleed_into_a_following_table(tmp_path: Path):
    text = (
        'project = "p"\nhost = "h"\n\n[[port]]\nname = "a"\nwant = 1\n'
        '\n[other]\nassigned = "not-a-port-field"\n'
    )
    path = _write(tmp_path, text)

    write_assigned(path, "a", 9999)

    text = path.read_text(encoding="utf-8")
    assert 'assigned = "not-a-port-field"' in text
    assert load_declaration(path).ports[0].assigned == 9999


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


def test_a_failed_write_leaves_the_existing_declaration_intact(tmp_path: Path, monkeypatch):
    # `.harbor.toml` is the human-owned half of the contract, in a repository
    # this tool does not own. A failed write must not truncate it.
    path = tmp_path / ".harbor.toml"
    path.write_text(FULL, encoding="utf-8")
    _truncating_disk_full(monkeypatch)

    with pytest.raises(OSError):
        write_assigned(path, "dashboard", 8100)

    assert path.read_text(encoding="utf-8") == FULL
    assert [entry.name for entry in tmp_path.iterdir()] == [".harbor.toml"]
