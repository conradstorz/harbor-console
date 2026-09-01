from datetime import date
from pathlib import Path

import pytest

from harbor_console.ports import keys
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
        Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5)),
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
        'project = "gte"\nname = "console"\nhost = "hpz440"\n'
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
        'project = "gte"\nname = "console"\nhost = "hpz440"\n'
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
    save_leases(path, [Lease("gte", "web", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5))])
    original = path.read_text(encoding="utf-8")
    _truncating_disk_full(monkeypatch)

    with pytest.raises(OSError):
        save_leases(path, [Lease("other", "web", "hpz440", "0.0.0.0", 8100, date(2026, 8, 9))])

    assert path.read_text(encoding="utf-8") == original
    assert [entry.name for entry in tmp_path.iterdir()] == ["services.toml"]
