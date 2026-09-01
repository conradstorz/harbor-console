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
