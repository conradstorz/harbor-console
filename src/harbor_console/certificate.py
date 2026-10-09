"""The certificate the edge serves on the tailnet address."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Certificate:
    """What the edge presented: every name it covers and when it expires (UTC)."""

    names: tuple[str, ...]
    not_after: datetime


@dataclass(frozen=True)
class CertificateUnavailable:
    """Why the certificate could not be read. Never raised, always returned."""

    reason: str
