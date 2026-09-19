"""Live container state: names, labels, published ports, networks.

Labels are how a container declares itself (see `directory.py`): Traefik's
`traefik.*` labels for an HTTP route, `harbor.kind` for everything else. The
page can only report an undeclared container if it can read labels, which is
why this collector uses `docker inspect` rather than `docker ps --format`.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field


class _Unavailable(tuple):
    """A distinguishable empty result: falsy, iterable, and identity-checkable."""


#: Returned when Docker could not be asked at all, so a caller can tell that
#: apart from "asked, and nothing is running" -- the difference decides whether
#: the page may claim a container is undeclared.
DOCKER_UNAVAILABLE = _Unavailable()

#: A bound on each docker call. This runs inside the prober thread on every
#: cycle; a wedged daemon must not freeze the last good snapshot in place.
DOCKER_TIMEOUT_SECONDS = 2.0

IPV6_ANY = "::"
IPV4_ANY = "0.0.0.0"


@dataclass(frozen=True)
class Container:
    """One running container: what it publishes and what it declares."""

    name: str
    published: tuple[tuple[str, int], ...]
    labels: dict[str, str] = field(default_factory=dict)
    networks: frozenset[str] = frozenset()


def running_containers(
    run: Callable[..., object] = subprocess.run,
    timeout: float = DOCKER_TIMEOUT_SECONDS,
) -> tuple[Container, ...]:
    """Collect running containers. Returns DOCKER_UNAVAILABLE if Docker cannot be read.

    Two calls: `docker ps -q` for the running set, then one `docker inspect`
    over all of it. Either failing, hanging, or answering something that is
    not JSON is the same outage. One malformed entry inside good JSON is
    skipped, the way `listening.py` skips one bad socket: the rest is still
    evidence.
    """
    try:
        listed = run(
            ["docker", "ps", "-q"],
            check=False, capture_output=True, text=True, timeout=timeout,
        )
        if listed.returncode != 0:  # type: ignore[attr-defined]
            return DOCKER_UNAVAILABLE
        ids = listed.stdout.split()  # type: ignore[attr-defined]
        if not ids:
            return ()
        inspected = run(
            ["docker", "inspect", *ids],
            check=False, capture_output=True, text=True, timeout=timeout,
        )
        if inspected.returncode != 0:  # type: ignore[attr-defined]
            return DOCKER_UNAVAILABLE
        entries = json.loads(inspected.stdout)  # type: ignore[attr-defined]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return DOCKER_UNAVAILABLE

    if not isinstance(entries, list):
        return DOCKER_UNAVAILABLE

    containers = []
    for entry in entries:
        container = _parse(entry)
        if container is not None:
            containers.append(container)
    return tuple(sorted(containers, key=lambda item: item.name))


def _parse(entry: object) -> Container | None:
    """One inspect entry to one Container; None when it has no usable name."""
    if not isinstance(entry, dict):
        return None
    name = entry.get("Name")
    if not isinstance(name, str) or not name:
        return None
    config = entry.get("Config") if isinstance(entry.get("Config"), dict) else {}
    labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
    settings = (
        entry.get("NetworkSettings")
        if isinstance(entry.get("NetworkSettings"), dict)
        else {}
    )
    ports = settings.get("Ports") if isinstance(settings.get("Ports"), dict) else {}
    networks = settings.get("Networks") if isinstance(settings.get("Networks"), dict) else {}
    return Container(
        name=name.lstrip("/"),
        published=_publish_pairs(ports),
        labels={str(k): str(v) for k, v in labels.items()},
        networks=frozenset(str(n) for n in networks),
    )


def _publish_pairs(ports: dict) -> tuple[tuple[str, int], ...]:
    """Host `(addr, port)` pairs from inspect's `NetworkSettings.Ports`."""
    pairs: set[tuple[str, int]] = set()
    for key, bindings in ports.items():
        if not str(key).endswith("/tcp") or not isinstance(bindings, list):
            continue
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            addr = str(binding.get("HostIp", ""))
            addr = IPV4_ANY if addr in ("", IPV6_ANY) else addr
            try:
                pairs.add((addr, int(binding.get("HostPort"))))
            except (TypeError, ValueError):
                continue
    return tuple(sorted(pairs))
