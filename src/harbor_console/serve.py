"""What `tailscale serve` fronts, and the true port behind it.

A serve proxy is the one thing on this host that holds a tailnet port without
being a container and without being anybody's lease: `tailscaled` binds it, and
forwards to a backend that usually *is* declared. Port 8443 on hpz440 fronts
`127.0.0.1:8080`, which is gte's leased console -- so the port looks
undeclared, is undeclared, and is nonetheless serving a declared service.

Reporting the front alone would send an operator hunting for a process that
does not exist. This collector supplies the other half, so `reconcile` can name
the backend and whatever declares it.

Degrades quietly, like every collector but `tailnet.py`: no tailscale, a bad
exit, unreadable JSON and a hang all yield no proxies. Absence is reported as
*absence of knowledge* by `reconcile`, never as "nothing is proxying it" --
that claim would be false in exactly the case this collector failed.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

#: A bound on `tailscale serve status --json`. This runs in the prober thread
#: on every cycle, like `docker ps`, so the reasoning in `DOCKER_TIMEOUT_SECONDS`
#: applies unchanged: a wedged binary with no bound freezes the last good
#: snapshot in place while it is still served as current.
SERVE_TIMEOUT_SECONDS = 2.0

#: Names the ledger never records. It records addresses, so a backend written
#: as `localhost` has to be resolved to the address a lease could match.
_LOOPBACK_NAMES = ("localhost", "localhost.localdomain")


#: The port each scheme leaves off a URL.
_DEFAULT_PORTS = {"https": 443, "http": 80}


@dataclass(frozen=True)
class Proxy:
    """One `tailscale serve` front and the backend it forwards to.

    `front_host` and `scheme` default to a bare, HTTPS front so a `Proxy` can
    still be built from the backend half alone, which is all the drift rule
    needs. `url` is the half a reader needs: the address a browser can use.
    """

    port: int
    path: str
    backend_addr: str
    backend_port: int
    front_host: str = ""
    scheme: str = "https"

    @property
    def url(self) -> str | None:
        """The address to hand a reader, or None when the front is unnamed.

        `tailscale serve` terminates TLS for the MagicDNS name, so this is not
        merely a nicer way to write the leased port: an app that sets Secure
        cookies will not complete a login over plain HTTP at all, and this URL
        is then the only one that works.
        """
        if not self.front_host:
            return None
        if self.port == _DEFAULT_PORTS.get(self.scheme):
            return f"{self.scheme}://{self.front_host}{self.path}"
        return f"{self.scheme}://{self.front_host}:{self.port}{self.path}"


def serve_proxies(
    run: Callable[..., object] = subprocess.run,
    timeout: float = SERVE_TIMEOUT_SECONDS,
) -> tuple[Proxy, ...]:
    """Collect every HTTP(S) front `tailscale serve` is running."""
    try:
        result = run(
            ["tailscale", "serve", "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ()
    except (FileNotFoundError, OSError):
        return ()

    if result.returncode != 0:  # type: ignore[attr-defined]
        return ()

    try:
        status = json.loads(result.stdout or "{}")  # type: ignore[attr-defined]
    except (ValueError, TypeError):
        return ()
    if not isinstance(status, dict):
        return ()

    tcp = status.get("TCP") or {}
    proxies: list[Proxy] = []
    for front, config in (status.get("Web") or {}).items():
        host, port = _front(front)
        if port is None:
            continue
        handlers = (config or {}).get("Handlers") or {}
        for path, handler in handlers.items():
            backend = _backend((handler or {}).get("Proxy"))
            if backend is None:
                continue
            addr, backend_port = backend
            proxies.append(
                Proxy(
                    port,
                    str(path),
                    addr,
                    backend_port,
                    front_host=host,
                    scheme=_scheme(tcp, port),
                )
            )

    return tuple(sorted(proxies, key=lambda item: (item.port, item.path)))


def _front(front: str) -> tuple[str, int | None]:
    """The `(host, port)` a `host:port` key names.

    Rsplit, because the host half is a DNS name here rather than an address --
    but a key with no colon at all is not something to guess a port for.
    """
    host, sep, port = str(front).rpartition(":")
    if not sep:
        return "", None
    try:
        return host, int(port)
    except ValueError:
        return host, None


def _scheme(tcp: object, port: int) -> str:
    """Whether a front terminates TLS, from the `TCP` map serve publishes.

    Defaults to HTTPS, which is what `serve` does unless told otherwise and
    what an older daemon with no `TCP` map was doing. The default is also the
    safer error: an `https://` URL to a plain front fails loudly, where
    `http://` to a TLS front can be redirected or silently downgraded.
    """
    if not isinstance(tcp, dict):
        return "https"
    entry = tcp.get(str(port)) or {}
    if isinstance(entry, dict) and entry.get("HTTPS") is not True and entry.get("HTTP") is True:
        return "http"
    return "https"


def _backend(proxy: object) -> tuple[str, int] | None:
    """The `(addr, port)` a proxy target names, or None.

    Parsed rather than pattern-matched: the scheme can be `https+insecure`,
    which is what `serve` writes for a backend with a self-signed certificate,
    and matching on `http://` would miss it.

    A target with no explicit port is skipped. The point of this collector is
    to name the true port; inferring 80 or 443 from the scheme would be a
    guess printed as a fact, and `serve` writes the port in practice.
    """
    if not isinstance(proxy, str) or not proxy:
        return None
    try:
        parts = urlsplit(proxy)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host or port is None:
        return None
    if host in _LOOPBACK_NAMES:
        host = "127.0.0.1"
    return host, port
