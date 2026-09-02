"""The published-port scanner, exercised directly on compose text."""

from __future__ import annotations

from pathlib import Path

from harbor_console.ports.compose import published_ports


def _compose(tmp_path: Path, text: str) -> Path:
    (tmp_path / "docker-compose.yml").write_text(text, encoding="utf-8")
    return tmp_path


def test_a_dash_entry_at_the_same_indent_as_ports_is_still_a_published_port(
    tmp_path: Path,
):
    # Legal, common YAML: a block sequence may sit at the indentation of the key
    # that owns it. Closing the block on `indent <= ports_indent` made every
    # compose file written this way invisible to the drift auditor -- the one
    # net under a `.env` that has drifted from the ledger.
    root = _compose(
        tmp_path,
        "services:\n  a:\n    ports:\n    - \"8080:8080\"\n    - \"${HARBOR_PORT_WEB:-9999}:80\"\n",
    )

    found = published_ports(root)

    assert [entry.literal for entry in found if entry.literal] == [8080]
    assert [(entry.var, entry.default) for entry in found if entry.var] == [
        ("HARBOR_PORT_WEB", 9999)
    ]


def test_a_deeper_dash_entry_still_works(tmp_path: Path):
    root = _compose(
        tmp_path,
        'services:\n  a:\n    ports:\n      - "8080:8080"\n',
    )

    assert [entry.literal for entry in published_ports(root)] == [8080]


def test_a_sibling_key_still_closes_the_block(tmp_path: Path):
    # The block must still end at the next non-dash key at or above its own
    # indent, or a port-shaped scalar under `command:` would be read as a
    # published port and fabricate a drift report.
    root = _compose(
        tmp_path,
        'services:\n  a:\n    ports:\n    - "8080:8080"\n'
        '    command:\n      - "--listen=9999:9999"\n',
    )

    assert [entry.literal for entry in published_ports(root)] == [8080]


def test_a_dash_entry_shallower_than_the_key_closes_the_block(tmp_path: Path):
    root = _compose(
        tmp_path,
        'services:\n  a:\n      ports:\n  - "8080:8080"\n',
    )

    assert published_ports(root) == []


def test_a_tab_counts_as_one_indent_unit(tmp_path: Path):
    # Leading whitespace is counted character by character in both the key line
    # and its entries, so a file indented with tabs is measured consistently.
    root = _compose(tmp_path, 'services:\n\ta:\n\t\tports:\n\t\t- "8080:8080"\n')

    assert [entry.literal for entry in published_ports(root)] == [8080]
