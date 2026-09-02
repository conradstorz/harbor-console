"""The lease ledger: which project holds which (host, addr, port), and since when.

The ledger is the authority on what is *reserved*. Live state is only evidence
about what is *running*; a stopped service keeps its lease.

Nothing read back out of the file is taken on trust. `services.toml` is written
by this tool but sits on disk where a person can edit it, and every field goes
straight back out through `dumps_leases`, which interpolates values verbatim: a
`port` that is a float or a bool emits TOML no later command can load, and a
`granted` that is a string rather than a date literal raises deep inside the
emitter. So the types are checked here, where the file is read, and a bad one
is a `LedgerError` naming the file -- including when the file cannot be read or
decoded at all, so that `ports show` still reports rather than crashes.
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from harbor_console.ports.atomic import write_text_atomic
from harbor_console.ports.keys import MAX_PORT, MIN_PORT, addrs_overlap, is_port_number


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

#: The lease fields that are emitted back out inside TOML string quotes. A
#: non-string here reaches `dumps_leases` and is formatted by `str()`, which is
#: how an integer or a list ends up quoted in the ledger as though it had always
#: been a name.
_STRING_FIELDS = ("project", "name", "host", "addr")


def load_leases(path: Path) -> list[Lease]:
    """Read and validate the ledger. A missing file is an empty ledger."""
    if not path.exists():
        return []

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise LedgerError(f"{path}: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        # A ledger that exists but cannot be read -- permissions, a transient
        # I/O error, a non-UTF-8 rewrite by a hand editor -- used to escape as
        # a raw traceback and take `ports show` down with it. `show` is the one
        # command that stands alone precisely so it still answers when the rest
        # is broken, so an unreadable file is reported, not raised through.
        raise LedgerError(f"{path}: {exc}") from exc

    leases: list[Lease] = []
    for entry in data.get("lease", []):
        missing = [field for field in _FIELDS if field not in entry]
        if missing:
            raise LedgerError(f"{path}: lease missing {', '.join(missing)}")

        for field in _STRING_FIELDS:
            value = entry[field]
            if not isinstance(value, str):
                raise LedgerError(
                    f"{path}: lease {field} {value!r} is not a string"
                )

        port = entry["port"]
        if not is_port_number(port):
            raise LedgerError(
                f"{path}: lease port {port!r} is not usable; it must be a whole "
                f"number between {MIN_PORT} and {MAX_PORT}"
            )

        granted = entry["granted"]
        if not isinstance(granted, date):
            raise LedgerError(
                f"{path}: lease granted {granted!r} is not a date; write it as a "
                f"bare TOML date such as 2026-09-01, not as a quoted string"
            )

        leases.append(
            Lease(
                project=entry["project"],
                name=entry["name"],
                host=entry["host"],
                addr=entry["addr"],
                port=port,
                granted=granted,
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
    """Write the ledger, replacing it wholesale -- atomically.

    The ledger is the only record of who holds what; a failed write must leave
    the previous one readable rather than emptying it.
    """
    write_text_atomic(path, dumps_leases(leases))
