"""Pure naming and address-overlap rules shared by the ledger and the allocator.

This module imports nothing from the package so that both the data layer and the
decision layer can depend on it without a cycle.
"""

from __future__ import annotations

import re

ANY_ADDR = "0.0.0.0"

#: What every published variable starts with. Named so that a caller can ask
#: whether a port name derived *anything* beyond the prefix: a name made only of
#: punctuation slugs to nothing and would publish a bare `HARBOR_PORT_=`.
VAR_PREFIX = "HARBOR_PORT_"

#: The lowest and highest port a lease may record. Port 0 is excluded on
#: purpose: it means "let the kernel choose", which is not something a ledger
#: entry can name, publish through `.env` or hand to a compose file.
MIN_PORT = 1
MAX_PORT = 65535

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def is_port_number(value: object) -> bool:
    """Return True when `value` is a real integer inside the valid port range.

    `bool` is refused explicitly, because `True` satisfies `isinstance(x, int)`
    and would then be emitted into the ledger as `port = True` -- TOML booleans
    are lowercase, so that file is one no later command could load. A float is
    refused for a quieter reason: `int()` would truncate `8080.9` back to 8080
    without a word, and `.env` would publish `8080.9` to a compose file that
    cannot use it.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    return MIN_PORT <= value <= MAX_PORT


def addrs_overlap(a: str, b: str) -> bool:
    """Return True when two bind addresses contend for the same port.

    ``0.0.0.0`` claims every address on the host, so it overlaps anything. Two
    different specific addresses can each hold the same port number without
    conflict -- which is how a container published to one specific address
    holds 100.69.239.123:49152 without claiming 49152 from every other
    project.
    """
    if a == b:
        return True
    return ANY_ADDR in (a, b)


def env_var_name(port_name: str) -> str:
    """Derive the environment variable a declared port is published through.

    A port name with no letters or digits in it slugs to nothing and yields the
    bare `VAR_PREFIX`. That is not a usable variable name, and it is refused
    where declarations are read rather than papered over here -- this function
    is also how `.env` is written, and the two must agree.
    """
    slug = _NON_ALNUM.sub("_", port_name).strip("_").upper()
    return f"{VAR_PREFIX}{slug}"
