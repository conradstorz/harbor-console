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
) -> tuple[Container, ...]:
    """Collect running containers. Returns DOCKER_UNAVAILABLE if Docker cannot be read."""
    try:
        result = run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
            check=False,
            capture_output=True,
            text=True,
        )
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
        addr = IPV4_ANY if addr in ("", IPV6_ANY, "::") else addr
        pairs.append((addr, int(match.group("port"))))

    return tuple(sorted(set(pairs)))
