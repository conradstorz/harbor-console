"""The address harbor-console-web binds, and the only one it will accept.

Every other collector in this project degrades quietly on a hostile
environment. This one raises, because the page is an inventory of every service
on the host: a silent fallback to a broader address would publish it to the
whole LAN. Binding is the access control, which is why there is no login page.
See ADR 7.
"""

from __future__ import annotations

import ipaddress
import subprocess
from collections.abc import Callable


class TailnetUnavailable(Exception):
    """The host's Tailscale address could not be determined."""


def tailscale_address(run: Callable[..., object] = subprocess.run) -> str:
    """Return the host's Tailscale IPv4 address.

    Asks `tailscale` itself rather than guessing from an interface name or an
    address range, because it is the authority on its own address.
    """
    try:
        result = run(
            ["tailscale", "ip", "-4"],
            check=False,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError) as exc:
        raise TailnetUnavailable(f"could not run tailscale: {exc}") from exc

    if result.returncode != 0:  # type: ignore[attr-defined]
        raise TailnetUnavailable(
            f"tailscale ip -4 exited {result.returncode}"  # type: ignore[attr-defined]
        )

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]  # type: ignore[attr-defined]
    if not lines:
        raise TailnetUnavailable("tailscale ip -4 returned no address")

    candidate = lines[0]
    try:
        ipaddress.IPv4Address(candidate)
    except ValueError as exc:
        raise TailnetUnavailable(f"'{candidate}' is not an IPv4 address") from exc

    return candidate
