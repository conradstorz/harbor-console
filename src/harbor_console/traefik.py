"""What Traefik routes, from its own API.

Traefik is the only truth for which hostnames reach which containers, so the
page asks it rather than re-deriving routes from labels. The API is bound to
loopback by `deploy/traefik/compose.yaml`; nothing here is reachable from the
tailnet. Degrades to `TRAEFIK_UNAVAILABLE` on any failure, which the page
reports as absence of knowledge, never as "nothing is routed".

`/api` itself is gated by an HTTP Basic Auth middleware
(`deploy/traefik/dynamic/api.yml`): the loopback-only host publish does not
stop a container on the `harbor` Docker network from reaching this same
entrypoint as `traefik:8081`, which would otherwise hand over the whole edge
configuration -- every routed hostname, every backend, every middleware --
to anything running there. `credentials` is that gate's password, read from
disk by `webapp.read_traefik_credentials` and threaded through unchanged.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass


class _Unavailable(tuple):
    """A distinguishable empty result: falsy, iterable, and identity-checkable."""


TRAEFIK_UNAVAILABLE = _Unavailable()
TRAEFIK_API = "http://127.0.0.1:8081"
TRAEFIK_TIMEOUT_SECONDS = 2.0

_HOST = re.compile(r"Host\(`([^`]+)`\)")


@dataclass(frozen=True)
class Router:
    """One Traefik router: a hostname, the service behind it, and Traefik's verdict."""

    name: str
    host: str | None
    service: str
    enabled: bool
    error: str | None


def router_name(label_name: str) -> str:
    """The full Traefik name of a router declared in Docker labels."""
    return f"{label_name}@docker"


def traefik_routers(
    opener: Callable[..., object] = urllib.request.urlopen,
    base: str = TRAEFIK_API,
    timeout: float = TRAEFIK_TIMEOUT_SECONDS,
    credentials: tuple[str, str] | None = None,
) -> tuple[Router, ...]:
    """Collect every HTTP router Traefik knows, or TRAEFIK_UNAVAILABLE.

    A wrong or missing credential against a gated API is the same failure
    as an unreachable one: Traefik answers 401, `urlopen` raises
    `HTTPError`, and it folds into `TRAEFIK_UNAVAILABLE` below like any
    other reason the API could not be read.
    """
    url = f"{base}/api/http/routers"
    request: str | urllib.request.Request = url
    if credentials is not None:
        user, password = credentials
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        request = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
    try:
        with opener(request, timeout=timeout) as response:  # type: ignore[union-attr]
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException, urllib.error.URLError):
        return TRAEFIK_UNAVAILABLE

    if not isinstance(payload, list):
        return TRAEFIK_UNAVAILABLE

    routers = []
    for entry in payload:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        rule = str(entry.get("rule", ""))
        match = _HOST.search(rule)
        errors = entry.get("error")
        error = None
        if isinstance(errors, list) and errors:
            error = "; ".join(str(item) for item in errors)
        elif isinstance(errors, str) and errors:
            error = errors
        routers.append(
            Router(
                name=entry["name"],
                host=match.group(1) if match else None,
                service=str(entry.get("service", "")),
                enabled=entry.get("status") == "enabled",
                error=error,
            )
        )
    return tuple(sorted(routers, key=lambda item: item.name))
