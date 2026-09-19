"""The directory and its findings: what is declared, what is running, where they disagree.

Pure: containers, routers, listeners and probe results in; rows and findings
out. No I/O, so every rule is testable with plain values.

A container declares itself with labels and nothing else. `traefik.enable=true`
plus a router rule is an HTTP service; `harbor.kind` covers everything Traefik
does not route. Traefik is the truth for whether a route is live; this module
only joins its verdict to the container that asked for it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from harbor_console.docker import Container
from harbor_console.listening import Listener, addrs_overlap
from harbor_console.probe import Health
from harbor_console.traefik import TRAEFIK_UNAVAILABLE, Router, router_name

KIND_HTTP = "http"
KIND_TCP = "tcp"
KIND_INTERNAL = "internal"
KIND_EDGE = "edge"
KINDS = frozenset({KIND_TCP, KIND_INTERNAL, KIND_EDGE})

LABEL_ENABLE = "traefik.enable"
LABEL_KIND = "harbor.kind"
LABEL_PORT = "harbor.port"
LABEL_DESCRIPTION = "harbor.description"

STATE_UP = "UP"
STATE_DOWN = "DOWN"
STATE_ROUTE_ERROR = "ROUTE ERROR"
STATE_LISTENING = "LISTENING"
STATE_INTERNAL = "INTERNAL"
STATE_UNKNOWN = "UNKNOWN"

_RULE_LABEL = re.compile(r"^traefik\.http\.routers\.([^.]+)\.rule$")
_HOST = re.compile(r"Host\(`([^`]+)`\)")


@dataclass(frozen=True)
class Row:
    """One directory entry."""

    name: str
    kind: str
    target: str
    container: str
    description: str
    state: str


def declared_kind(container: Container) -> str | None:
    """Which kind a container declares, or None when it declares nothing."""
    if container.labels.get(LABEL_ENABLE, "").lower() == "true":
        return KIND_HTTP
    kind = container.labels.get(LABEL_KIND)
    return kind if kind in KINDS else None


def route_of(container: Container) -> tuple[str, str | None] | None:
    """The `(router name, host)` an HTTP container asks Traefik for.

    The router name is the label's, not the container's: it is the key
    Traefik reports under, and the name the page shows. With `traefik.enable`
    but no rule label Traefik would invent a router named for the container;
    the page names it the same way and leaves the host unknown, which
    `build_rows` reports as a route error rather than guessing a URL.
    """
    if declared_kind(container) != KIND_HTTP:
        return None
    for key, value in sorted(container.labels.items()):
        match = _RULE_LABEL.match(key)
        if match:
            host = _HOST.search(value)
            return match.group(1), host.group(1) if host else None
    return container.name, None


def build_rows(
    containers: Sequence[Container],
    routers: Sequence[Router],
    listeners: Sequence[Listener],
    health: Mapping[str, Health],
    probed: bool,
) -> tuple[Row, ...]:
    """One row per declared container, ordered by name."""
    by_name = {} if routers is TRAEFIK_UNAVAILABLE else {r.name: r for r in routers}
    rows = []
    for container in containers:
        kind = declared_kind(container)
        if kind is None:
            continue
        description = container.labels.get(LABEL_DESCRIPTION, "")
        if kind == KIND_HTTP:
            rows.append(_http_row(container, by_name, health, probed, description))
        elif kind == KIND_TCP:
            rows.append(_tcp_row(container, listeners, description))
        elif kind == KIND_EDGE:
            rows.append(_edge_row(container, listeners, description))
        else:
            rows.append(
                Row(container.name, KIND_INTERNAL, "", container.name, description, STATE_INTERNAL)
            )
    return tuple(sorted(rows, key=lambda row: row.name))


def _http_row(
    container: Container,
    routers: Mapping[str, Router],
    health: Mapping[str, Health],
    probed: bool,
    description: str,
) -> Row:
    name, host = route_of(container)  # type: ignore[misc] - kind is HTTP here
    router = routers.get(router_name(name))
    target = f"https://{host}/" if host else ""
    if host is None or (router is not None and not router.enabled):
        state = STATE_ROUTE_ERROR
    elif not probed:
        state = STATE_UNKNOWN
    else:
        result = health.get(name)
        state = STATE_UP if result is not None and result.up else STATE_DOWN
    return Row(name, KIND_HTTP, target, container.name, description, state)


def _tcp_row(container: Container, listeners: Sequence[Listener], description: str) -> Row:
    try:
        port = int(container.labels.get(LABEL_PORT, ""))
    except ValueError:
        return Row(container.name, KIND_TCP, "", container.name, description, STATE_ROUTE_ERROR)
    published = [pair for pair in container.published if pair[1] == port]
    if not published:
        # A label naming a port the container does not publish is a
        # declaration error, not a live port: there is no address to check
        # a listener against, so guessing "0.0.0.0" would match everything.
        return Row(container.name, KIND_TCP, "", container.name, description, STATE_ROUTE_ERROR)
    addr = published[0][0]
    held = any(
        listener.port == port and addrs_overlap(listener.addr, addr) for listener in listeners
    )
    return Row(
        container.name,
        KIND_TCP,
        f"{addr}:{port}",
        container.name,
        description,
        STATE_LISTENING if held else STATE_DOWN,
    )


def _edge_row(container: Container, listeners: Sequence[Listener], description: str) -> Row:
    held = all(
        any(
            listener.port == port and addrs_overlap(listener.addr, addr)
            for listener in listeners
        )
        for addr, port in container.published
    ) and bool(container.published)
    target = ", ".join(f"{addr}:{port}" for addr, port in container.published)
    return Row(
        container.name,
        KIND_EDGE,
        target,
        container.name,
        description,
        STATE_LISTENING if held else STATE_DOWN,
    )
