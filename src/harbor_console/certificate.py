"""The certificate the edge serves on the tailnet address.

Collects only. One TLS handshake to `<tailnet address>:443` with the page's
own route name as SNI, which is exactly what a browser on the tailnet does,
so what comes back is what every route is served with. Stdlib `ssl`, no new
dependency, a 2 s bound so a wedged edge cannot stall the prober.

Never raises. A handshake that fails for any reason -- refused, timed out,
expired, wrong name, not TLS at all -- is returned as `CertificateUnavailable`
with the reason, and the check in `checks.py` decides what that means. It
means failed: the edge not completing a handshake on its own address is
platform breakage, not missing evidence.
"""

from __future__ import annotations

import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

CERTIFICATE_TIMEOUT_SECONDS = 2.0
DEFAULT_SERVER_NAME = "harbor.hpz440.ohr3023.org"
TLS_PORT = 443


@dataclass(frozen=True)
class Certificate:
    """What the edge presented: every name it covers and when it expires (UTC)."""

    names: tuple[str, ...]
    not_after: datetime


@dataclass(frozen=True)
class CertificateUnavailable:
    """Why the certificate could not be read. Never raised, always returned."""

    reason: str


def _connect(address: str, port: int, server_name: str, timeout: float) -> dict:
    """Handshake and return `getpeercert()`. The one call that touches the network."""
    context = ssl.create_default_context()
    with socket.create_connection((address, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=server_name) as tls:
            return tls.getpeercert() or {}


def served_certificate(
    address: str,
    server_name: str = DEFAULT_SERVER_NAME,
    port: int = TLS_PORT,
    timeout: float = CERTIFICATE_TIMEOUT_SECONDS,
    connector: Callable[[str, int, str, float], dict] = _connect,
) -> Certificate | CertificateUnavailable:
    """The certificate served at `address:port` for `server_name`, or why not."""
    try:
        peer = connector(address, port, server_name, timeout)
    except (OSError, ValueError) as exc:
        # ssl.SSLError and socket.timeout are both OSError subclasses.
        return CertificateUnavailable(f"{exc.__class__.__name__}: {exc}")

    if not peer:
        return CertificateUnavailable("no certificate was presented")

    names = tuple(
        value for kind, value in peer.get("subjectAltName", ()) if kind == "DNS"
    )
    if not names:
        names = tuple(
            value
            for rdn in peer.get("subject", ())
            for key, value in rdn
            if key == "commonName"
        )

    try:
        not_after = datetime.fromtimestamp(
            ssl.cert_time_to_seconds(str(peer.get("notAfter", ""))), tz=timezone.utc
        )
    except (ValueError, OverflowError, OSError) as exc:
        return CertificateUnavailable(f"could not read the expiry: {exc}")

    return Certificate(names=names, not_after=not_after)
