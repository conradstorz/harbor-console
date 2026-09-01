from pathlib import Path

from harbor_console.ports.compose import published_ports
from harbor_console.ports.discovery import find_declarations, tree_root


def test_tree_root_prefers_the_environment_override(tmp_path: Path):
    assert tree_root(env={"HARBOR_TREE_ROOT": str(tmp_path)}) == tmp_path


def test_tree_root_defaults_to_the_parent_of_the_repo(tmp_path: Path):
    repo = tmp_path / "programming" / "harbor-console"
    repo.mkdir(parents=True)

    assert tree_root(env={}, start=repo) == tmp_path / "programming"


def test_find_declarations_scans_direct_children_only(tmp_path: Path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / ".harbor.toml").write_text("", encoding="utf-8")
    (tmp_path / "beta").mkdir()
    nested = tmp_path / "beta" / "deep"
    nested.mkdir()
    (nested / ".harbor.toml").write_text("", encoding="utf-8")

    found = find_declarations(tmp_path)

    assert found == [tmp_path / "alpha" / ".harbor.toml"]


def test_find_declarations_is_sorted_and_tolerates_a_missing_root(tmp_path: Path):
    for name in ("zeta", "alpha"):
        (tmp_path / name).mkdir()
        (tmp_path / name / ".harbor.toml").write_text("", encoding="utf-8")

    assert [path.parent.name for path in find_declarations(tmp_path)] == ["alpha", "zeta"]
    assert find_declarations(tmp_path / "nope") == []


def test_published_ports_reads_variables_defaults_and_literals(tmp_path: Path):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    ports:\n"
        '      - "${HARBOR_PORT_WEB:-8080}:8080"\n'
        '      - "9000:9000"\n'
        '      - "100.69.239.123:49152:8080"\n',
        encoding="utf-8",
    )

    found = published_ports(tmp_path)

    assert (found[0].var, found[0].default) == ("HARBOR_PORT_WEB", 8080)
    assert found[1].literal == 9000
    assert found[2].literal == 49152


def test_published_ports_covers_every_compose_variant(tmp_path: Path):
    (tmp_path / "docker-compose.yml").write_text(
        'services:\n  a:\n    ports:\n      - "1:1"\n', encoding="utf-8"
    )
    (tmp_path / "docker-compose.prod.yml").write_text(
        'services:\n  a:\n    ports:\n      - "2:2"\n', encoding="utf-8"
    )

    names = sorted(port.file.name for port in published_ports(tmp_path))

    assert names == ["docker-compose.prod.yml", "docker-compose.yml"]


def test_published_ports_reads_unquoted_short_syntax(tmp_path: Path):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    ports:\n"
        "      - 8080:8080\n"
        "      - 127.0.0.1:9090:9090\n",
        encoding="utf-8",
    )

    found = published_ports(tmp_path)

    assert [port.literal for port in found] == [8080, 9090]


def test_published_ports_reads_protocol_suffixes(tmp_path: Path):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    ports:\n"
        '      - "8080:8080/udp"\n'
        '      - "9090:9090/tcp"\n',
        encoding="utf-8",
    )

    found = published_ports(tmp_path)

    assert [port.literal for port in found] == [8080, 9090]


def test_published_ports_reads_single_quoted_values(tmp_path: Path):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  web:\n    ports:\n      - '8080:8080'\n", encoding="utf-8"
    )

    found = published_ports(tmp_path)

    assert [port.literal for port in found] == [8080]


def test_published_ports_reads_bare_compose_spec_filenames(tmp_path: Path):
    (tmp_path / "compose.yaml").write_text(
        'services:\n  web:\n    ports:\n      - "8080:8080"\n', encoding="utf-8"
    )

    found = published_ports(tmp_path)

    assert [port.literal for port in found] == [8080]


def test_published_ports_ignores_an_image_like_list_entry(tmp_path: Path):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  cache:\n    entrypoint:\n      - redis:7.2\n", encoding="utf-8"
    )

    found = published_ports(tmp_path)

    assert found == []


def test_published_ports_ignores_a_commented_out_ports_entry(tmp_path: Path):
    (tmp_path / "docker-compose.yml").write_text(
        'services:\n  web:\n    # ports:\n    #   - "8080:8080"\n', encoding="utf-8"
    )

    found = published_ports(tmp_path)

    assert found == []
