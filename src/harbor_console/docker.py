"""Live container state, for reconciliation against the lease ledger.

Answers only "which container publishes which host port". Container names are
how a running service is matched to a declared lease; the ledger itself carries
no container field, so the match is by port, with the name used to report a
mismatch when it happens to equal a project's name.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass


class _Unavailable(tuple):
    """A distinguishable empty result: falsy, iterable, and identity-checkable."""


#: Returned when Docker could not be asked at all, so a caller can tell that
#: apart from "asked, and nothing is running" -- the difference decides whether
#: the page may claim a service is undeclared. It is an empty tuple subclass, so
#: every consumer can iterate it without caring, while `is DOCKER_UNAVAILABLE`
#: still distinguishes it from an ordinary empty result.
DOCKER_UNAVAILABLE = _Unavailable()

#: A bound on `docker ps`. This runs inside the prober thread on every
#: collection cycle, unlike `tailnet.py`'s one-shot startup check -- so a
#: wedged daemon here does not just delay one boot, it blocks the thread
#: forever and freezes the last good snapshot in place. Served as a 200 with
#: `probed=True` and `docker_available=True`, that snapshot's age keeps
#: growing while the allocator treats it as current evidence. A timeout is
#: the same failure as any other Docker outage: refuse, and let the next
#: cycle try again.
DOCKER_TIMEOUT_SECONDS = 2.0

IPV6_ANY = "::"
IPV4_ANY = "0.0.0.0"

#: Matches the published half of `0.0.0.0:8080->8080/tcp` and `:::8080->8080/tcp`.
_PUBLISHED = re.compile(r"^(?P<addr>.*):(?P<port>\d+)->")


@dataclass(frozen=True)
class Container:
    """One running container and the host ports it publishes."""

    name: str
    published: tuple[tuple[str, int], ...]


def running_containers(
    run: Callable[..., object] = subprocess.run,
    timeout: float = DOCKER_TIMEOUT_SECONDS,
) -> tuple[Container, ...]:
    """Collect running containers. Returns DOCKER_UNAVAILABLE if Docker cannot be read.

    A `docker ps` that hangs is treated exactly like one that is missing or
    exits non-zero: without the bound, a wedged daemon would block the caller
    forever and the last good snapshot would keep being served as current,
    since nothing else in this collector's contract distinguishes "still
    running" from "will never return".
    """
    try:
        result = run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return DOCKER_UNAVAILABLE
    except (FileNotFoundError, OSError):
        return DOCKER_UNAVAILABLE

    if result.returncode != 0:  # type: ignore[attr-defined]
        return DOCKER_UNAVAILABLE

    containers = []
    for line in result.stdout.splitlines():  # type: ignore[attr-defined]
        if not line.strip():
            continue
        name, _, ports = line.partition("\t")
        containers.append(Container(name=name.strip(), published=_publish_pairs(ports)))

    return tuple(sorted(containers, key=lambda item: item.name))


def _publish_pairs(ports: str) -> tuple[tuple[str, int], ...]:
    """Parse the published `addr:port->container/proto` entries from one line."""
    pairs: list[tuple[str, int]] = []
    for entry in ports.split(","):
        entry = entry.strip()
        if "->" not in entry:
            continue
        match = _PUBLISHED.match(entry)
        if match is None:
            continue
        addr = match.group("addr")
        if addr.startswith("[") and addr.endswith("]"):
            addr = addr[1:-1]
        addr = IPV4_ANY if addr in ("", IPV6_ANY) else addr
        pairs.append((addr, int(match.group("port"))))

    return tuple(sorted(set(pairs)))
