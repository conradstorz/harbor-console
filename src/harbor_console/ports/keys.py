"""Pure naming and address-overlap rules shared by the ledger and the allocator.

This module imports nothing from the package so that both the data layer and the
decision layer can depend on it without a cycle.
"""

from __future__ import annotations

import re

ANY_ADDR = "0.0.0.0"

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def addrs_overlap(a: str, b: str) -> bool:
    """Return True when two bind addresses contend for the same port.

    ``0.0.0.0`` claims every address on the host, so it overlaps anything. Two
    different specific addresses can each hold the same port number without
    conflict -- which is how ARM holds 100.69.239.123:49152 without claiming
    49152 from every other project.
    """
    if a == b:
        return True
    return ANY_ADDR in (a, b)


def env_var_name(port_name: str) -> str:
    """Derive the environment variable a declared port is published through."""
    slug = _NON_ALNUM.sub("_", port_name).strip("_").upper()
    return f"HARBOR_PORT_{slug}"
