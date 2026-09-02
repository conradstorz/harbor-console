from datetime import date
from pathlib import Path

import pytest

from harbor_console.ports import keys
from harbor_console.ports.declaration import DeclarationError, load_declaration
from harbor_console.ports.ledger import (
    Lease,
    LedgerError,
    dumps_leases,
    load_leases,
    save_leases,
)


def test_env_var_name_uppercases_and_replaces_punctuation():
    assert keys.env_var_name("dashboard") == "HARBOR_PORT_DASHBOARD"
    assert keys.env_var_name("web-ui.2") == "HARBOR_PORT_WEB_UI_2"


def test_any_addr_overlaps_everything_but_specifics_do_not_collide():
    assert keys.addrs_overlap("0.0.0.0", "100.69.239.123")
    assert keys.addrs_overlap("100.69.239.123", "0.0.0.0")
    assert keys.addrs_overlap("127.0.0.1", "127.0.0.1")
    assert not keys.addrs_overlap("127.0.0.1", "100.69.239.123")


def test_load_missing_file_returns_empty_list(tmp_path: Path):
    assert load_leases(tmp_path / "services.toml") == []


def test_round_trip_preserves_every_field(tmp_path: Path):
    leases = [
        Lease("acme", "console", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5)),
        Lease("arm", "web", "hpz440", "100.69.239.123", 49152, date(2026, 8, 1)),
    ]
    path = tmp_path / "services.toml"
    save_leases(path, leases)

    assert load_leases(path) == leases


def test_dumps_is_deterministic_and_sorted_by_host_port(tmp_path: Path):
    unsorted = [
        Lease("b", "x", "hpz440", "0.0.0.0", 8200, date(2026, 1, 2)),
        Lease("a", "y", "hpz440", "0.0.0.0", 8100, date(2026, 1, 1)),
    ]
    text = dumps_leases(unsorted)

    assert text.index("8100") < text.index("8200")
    assert dumps_leases(unsorted) == text


def test_duplicate_exact_key_is_a_hard_error(tmp_path: Path):
    path = tmp_path / "services.toml"
    path.write_text(
        "[[lease]]\n"
        'project = "acme"\nname = "console"\nhost = "hpz440"\n'
        'addr = "0.0.0.0"\nport = 8080\ngranted = 2026-07-05\n'
        "\n[[lease]]\n"
        'project = "imageharbor"\nname = "dashboard"\nhost = "hpz440"\n'
        'addr = "0.0.0.0"\nport = 8080\ngranted = 2026-08-09\n',
        encoding="utf-8",
    )

    with pytest.raises(LedgerError, match="8080"):
        load_leases(path)


def test_overlapping_addresses_on_one_port_is_a_hard_error(tmp_path: Path):
    path = tmp_path / "services.toml"
    path.write_text(
        "[[lease]]\n"
        'project = "acme"\nname = "console"\nhost = "hpz440"\n'
        'addr = "0.0.0.0"\nport = 8080\ngranted = 2026-07-05\n'
        "\n[[lease]]\n"
        'project = "other"\nname = "web"\nhost = "hpz440"\n'
        'addr = "100.69.239.123"\nport = 8080\ngranted = 2026-08-09\n',
        encoding="utf-8",
    )

    with pytest.raises(LedgerError, match="8080"):
        load_leases(path)


def test_same_port_on_different_hosts_is_fine(tmp_path: Path):
    leases = [
        Lease("a", "web", "hpz440", "0.0.0.0", 8100, date(2026, 1, 1)),
        Lease("b", "web", "other", "0.0.0.0", 8100, date(2026, 1, 1)),
    ]
    path = tmp_path / "services.toml"
    save_leases(path, leases)

    assert len(load_leases(path)) == 2


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


def test_a_failed_save_leaves_the_existing_ledger_intact(tmp_path: Path, monkeypatch):
    # The ledger is the only record of who holds what. Emptying it would free
    # every port at once, which is the worst possible outcome of a full disk.
    path = tmp_path / "services.toml"
    save_leases(path, [Lease("acme", "web", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5))])
    original = path.read_text(encoding="utf-8")
    _truncating_disk_full(monkeypatch)

    with pytest.raises(OSError):
        save_leases(path, [Lease("other", "web", "hpz440", "0.0.0.0", 8100, date(2026, 8, 9))])

    assert path.read_text(encoding="utf-8") == original
    assert [entry.name for entry in tmp_path.iterdir()] == ["services.toml"]


def _lease_text(**overrides: str) -> str:
    """One `[[lease]]` block, with any field replaced by a raw TOML value."""
    fields = {
        "project": '"acme"',
        "name": '"console"',
        "host": '"hpz440"',
        "addr": '"0.0.0.0"',
        "port": "8080",
        "granted": "2026-07-05",
    }
    fields.update(overrides)
    body = "".join(f"{key} = {value}\n" for key, value in fields.items())
    return f"[[lease]]\n{body}"


def test_an_unreadable_ledger_is_a_ledger_error(tmp_path: Path, monkeypatch):
    # `ports show` is deliberately independent of declaration loading so that it
    # still answers when everything else is broken. An `OSError` escaping the
    # loader -- a permissions problem, a transient I/O error -- took it down
    # with a raw traceback instead of a reported one.
    path = tmp_path / "services.toml"
    path.write_text(_lease_text(), encoding="utf-8")

    def refuse(self: Path, encoding: str | None = None, **kwargs) -> str:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", refuse)

    with pytest.raises(LedgerError, match="Permission denied"):
        load_leases(path)


def test_a_non_utf8_ledger_is_a_ledger_error(tmp_path: Path):
    # `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so it escaped
    # the loader untouched.
    path = tmp_path / "services.toml"
    path.write_bytes(_lease_text(project='"caf\u00e9"').encode("cp1252"))

    with pytest.raises(LedgerError, match="codec|decode"):
        load_leases(path)


def test_a_float_port_in_the_ledger_is_refused(tmp_path: Path):
    # `int(entry["port"])` truncated this silently, so a hand-edited `8080.9`
    # loaded as 8080 and the ledger and the file disagreed from then on.
    path = tmp_path / "services.toml"
    path.write_text(_lease_text(port="8080.9"), encoding="utf-8")

    with pytest.raises(LedgerError, match="port"):
        load_leases(path)


def test_a_boolean_port_in_the_ledger_is_refused(tmp_path: Path):
    path = tmp_path / "services.toml"
    path.write_text(_lease_text(port="true"), encoding="utf-8")

    with pytest.raises(LedgerError, match="port"):
        load_leases(path)


def test_a_numeric_string_port_in_the_ledger_is_refused(tmp_path: Path):
    # `int("8080")` accepted this too, so the ledger held a port whose type
    # depended on which command had last written it.
    path = tmp_path / "services.toml"
    path.write_text(_lease_text(port='"8080"'), encoding="utf-8")

    with pytest.raises(LedgerError, match="port"):
        load_leases(path)


def test_a_port_outside_the_range_is_refused(tmp_path: Path):
    for value in ("0", "-1", "65536"):
        path = tmp_path / f"services-{value}.toml"
        path.write_text(_lease_text(port=value), encoding="utf-8")

        with pytest.raises(LedgerError, match="port"):
            load_leases(path)


def test_a_quoted_granted_date_is_refused(tmp_path: Path):
    # A hand-edited `granted = "2026-09-01"` is a string, not a TOML date
    # literal. It loaded without complaint and then raised `AttributeError`
    # deep inside `dumps_leases`, on the next run, in a different command.
    path = tmp_path / "services.toml"
    path.write_text(_lease_text(granted='"2026-09-01"'), encoding="utf-8")

    with pytest.raises(LedgerError, match="granted"):
        load_leases(path)


def test_a_non_string_lease_field_is_refused(tmp_path: Path):
    # These four are interpolated between quotes by `dumps_leases`, which would
    # write `project = "42"` and turn a hand-editing slip into a permanent one.
    for field in ("project", "name", "host", "addr"):
        path = tmp_path / f"services-{field}.toml"
        path.write_text(_lease_text(**{field: "42"}), encoding="utf-8")

        with pytest.raises(LedgerError, match=field):
            load_leases(path)


def test_a_boolean_want_never_reaches_the_ledger(tmp_path: Path):
    # The invariant, end to end: a declaration this tool does not own asks for
    # `want = true`, and is refused where it is read. Had it not been, the
    # emitter would have written `port    = True` -- shown here -- and the
    # ledger would have been one no later command could load, bricking the tool
    # until somebody hand-edited `services.toml`.
    declaration = tmp_path / ".harbor.toml"
    declaration.write_text(
        'project = "p"\nhost = "h"\n\n[[port]]\nname = "web"\nwant = true\n',
        encoding="utf-8",
    )

    with pytest.raises(DeclarationError, match="want"):
        load_declaration(declaration)

    would_have_been = dumps_leases([Lease("p", "web", "h", "0.0.0.0", True, date(2026, 9, 1))])
    assert "port    = True" in would_have_been

    ledger = tmp_path / "services.toml"
    ledger.write_text(would_have_been, encoding="utf-8")
    with pytest.raises(LedgerError):
        load_leases(ledger)
