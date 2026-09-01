"""The lease ledger: which project holds which (host, addr, port), and since when.

The ledger is the authority on what is *reserved*. Live state is only evidence
about what is *running*; a stopped service keeps its lease.
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from harbor_console.ports.keys import addrs_overlap


class LedgerError(Exception):
    """The ledger is unreadable or self-contradictory."""


@dataclass(frozen=True)
class Lease:
    """One granted port, held by one project until it is released."""

    project: str
    name: str
    host: str
    addr: str
    port: int
    granted: date


_FIELDS = ("project", "name", "host", "addr", "port", "granted")


def load_leases(path: Path) -> list[Lease]:
    """Read and validate the ledger. A missing file is an empty ledger."""
    if not path.exists():
        return []

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise LedgerError(f"{path}: {exc}") from exc

    leases: list[Lease] = []
    for entry in data.get("lease", []):
        missing = [field for field in _FIELDS if field not in entry]
        if missing:
            raise LedgerError(f"{path}: lease missing {', '.join(missing)}")
        leases.append(
            Lease(
                project=entry["project"],
                name=entry["name"],
                host=entry["host"],
                addr=entry["addr"],
                port=int(entry["port"]),
                granted=entry["granted"],
            )
        )

    _reject_overlaps(path, leases)
    return leases


def _reject_overlaps(path: Path, leases: Sequence[Lease]) -> None:
    """A ledger that contradicts itself is an error, not a warning."""
    for i, a in enumerate(leases):
        for b in leases[i + 1 :]:
            if a.host == b.host and a.port == b.port and addrs_overlap(a.addr, b.addr):
                raise LedgerError(
                    f"{path}: {a.host} port {a.port} claimed twice "
                    f"({a.project}/{a.addr} and {b.project}/{b.addr})"
                )


def dumps_leases(leases: Sequence[Lease]) -> str:
    """Emit the ledger as TOML, deterministically ordered.

    Hand-rolled because the standard library reads TOML but cannot write it, and
    this project takes no new runtime dependency. The ledger is entirely
    machine-owned, so there are no comments or formatting to preserve.
    """
    ordered = sorted(leases, key=lambda lease: (lease.host, lease.port, lease.addr))
    blocks = []
    for lease in ordered:
        blocks.append(
            "[[lease]]\n"
            f'project = "{lease.project}"\n'
            f'name    = "{lease.name}"\n'
            f'host    = "{lease.host}"\n'
            f'addr    = "{lease.addr}"\n'
            f"port    = {lease.port}\n"
            f"granted = {lease.granted.isoformat()}\n"
        )
    header = (
        "# Harbor Console port ledger. Written by `harbor-console ports sync`.\n"
        "# The authority on which project holds which (host, addr, port).\n\n"
    )
    return header + "\n".join(blocks)


def save_leases(path: Path, leases: Sequence[Lease]) -> None:
    """Write the ledger, replacing it wholesale."""
    path.write_text(dumps_leases(leases), encoding="utf-8")
