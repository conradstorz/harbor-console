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

from harbor_console.docker import DOCKER_UNAVAILABLE, Container
from harbor_console.listening import PROTO_TCP, Listener, addrs_overlap
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


def route_url(host: str) -> str:
    """The URL a declared HTTP route is reachable at."""
    return f"https://{host}/"


def build_rows(
    containers: Sequence[Container],
    routers: Sequence[Router],
    listeners: Sequence[Listener],
    health: Mapping[str, Health],
    probed: bool,
) -> tuple[Row, ...]:
    """One row per declared container, ordered by name."""
    traefik_available = routers is not TRAEFIK_UNAVAILABLE
    by_name = {r.name: r for r in routers} if traefik_available else {}
    rows = []
    for container in containers:
        kind = declared_kind(container)
        if kind is None:
            continue
        description = container.labels.get(LABEL_DESCRIPTION, "")
        if kind == KIND_HTTP:
            rows.append(
                _http_row(container, by_name, traefik_available, health, probed, description)
            )
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
    traefik_available: bool,
    health: Mapping[str, Health],
    probed: bool,
    description: str,
) -> Row:
    """One HTTP row. Traefik's verdict decides the state before the probe does.

    A router Traefik does not report is a route error, not a down service:
    the container asked for a route and did not get one, most often by not
    being on the harbor network. That is only knowable when Traefik answered
    -- an empty `routers` because it could not be asked says nothing, so the
    probe decides, exactly as it did before there was a proxy.
    """
    name, host = route_of(container)  # type: ignore[misc] - kind is HTTP here
    router = routers.get(router_name(name))
    target = route_url(host) if host else ""
    if (
        host is None
        or (traefik_available and router is None)
        or (router is not None and not router.enabled)
    ):
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
    # A container may publish the same declared port on more than one
    # address (a specific address plus loopback, say). The service is
    # reachable if a listener overlaps *any* of them, not just the first
    # Docker happened to list -- checking only that one could report DOWN
    # for a service that is, in fact, up on a different published address.
    held = any(
        listener.proto == PROTO_TCP
        and listener.port == port
        and addrs_overlap(listener.addr, addr)
        for addr, _ in published
        for listener in listeners
    )
    target = ", ".join(f"{addr}:{port}" for addr, _ in published)
    return Row(
        container.name,
        KIND_TCP,
        target,
        container.name,
        description,
        STATE_LISTENING if held else STATE_DOWN,
    )


def _edge_row(container: Container, listeners: Sequence[Listener], description: str) -> Row:
    held = all(
        any(
            listener.proto == PROTO_TCP
            and listener.port == port
            and addrs_overlap(listener.addr, addr)
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


UNDECLARED_CONTAINER = "undeclared-container"
BYPASSES_PROXY = "bypasses-proxy"
ROUTE_ERROR = "route-error"
UNDECLARED_TAILNET_LISTENER = "undeclared-tailnet-listener"

#: Linux's default local port range. A tailnet listener in here is a
#: kernel-assigned port nobody chose -- tailscaled's own peerapi lands in it
#: on a different port after every restart -- not a deliberate publish.
EPHEMERAL_MIN = 32768
EPHEMERAL_MAX = 60999


@dataclass(frozen=True)
class Finding:
    """One way the declarations and the host disagree."""

    kind: str
    detail: str


def find_findings(
    containers: Sequence[Container],
    routers: Sequence[Router],
    listeners: Sequence[Listener],
    tailnet_address: str | None,
    own_port: int | None = None,
) -> tuple[Finding, ...]:
    """Every disagreement, in a stable order.

    `own_port` is this page's own bind on the tailnet address: a host process
    no container publishes, and the one such listener that is declared by
    being this program.

    Container findings need container evidence and are withheld when Docker
    could not be read; route findings need Traefik's verdict and are withheld
    when it could not be asked. Absence of evidence is never a finding.
    """
    findings: list[Finding] = []
    docker_available = containers is not DOCKER_UNAVAILABLE
    traefik_available = routers is not TRAEFIK_UNAVAILABLE

    if docker_available:
        for container in sorted(containers, key=lambda c: c.name):
            if declared_kind(container) is None:
                findings.append(
                    Finding(
                        UNDECLARED_CONTAINER,
                        f"container '{container.name}' carries no {LABEL_ENABLE} or {LABEL_KIND} label",
                    )
                )
        for container in sorted(containers, key=lambda c: c.name):
            for addr, port in _unaccounted_ports(container):
                findings.append(
                    Finding(
                        BYPASSES_PROXY,
                        f"container '{container.name}' publishes {addr}:{port}, "
                        f"which no {LABEL_KIND}={KIND_TCP} label accounts for",
                    )
                )

    if docker_available and traefik_available:
        known = {router.name: router for router in routers}
        for container in sorted(containers, key=lambda c: c.name):
            route = route_of(container)
            if route is None:
                continue
            name = router_name(route[0])
            router = known.get(name)
            if router is None:
                findings.append(
                    Finding(
                        ROUTE_ERROR,
                        f"container '{container.name}' asks for router {name}, which "
                        "Traefik does not report; is it on the harbor network?",
                    )
                )
            elif not router.enabled:
                findings.append(
                    Finding(ROUTE_ERROR, f"router {name} is disabled: {router.error or 'no reason given'}")
                )

    if docker_available and tailnet_address is not None:
        published = [pair for container in containers for pair in container.published]
        for addr, port in sorted(
            {(l.addr, l.port) for l in listeners
             if l.proto == PROTO_TCP and l.addr == tailnet_address}
        ):
            if port == own_port or EPHEMERAL_MIN <= port <= EPHEMERAL_MAX:
                continue
            if any(p == port and addrs_overlap(a, addr) for a, p in published):
                continue
            findings.append(
                Finding(
                    UNDECLARED_TAILNET_LISTENER,
                    f"{addr}:{port} is listening on the tailnet, and no container publishes it",
                )
            )

    return tuple(findings)


def _unaccounted_ports(container: Container) -> list[tuple[str, int]]:
    """Published host ports no label explains. The edge may publish anything."""
    kind = declared_kind(container)
    if kind == KIND_EDGE:
        return []
    allowed: set[int] = set()
    if kind == KIND_TCP:
        try:
            allowed.add(int(container.labels.get(LABEL_PORT, "")))
        except ValueError:
            pass
    return [pair for pair in container.published if pair[1] not in allowed]
