"""The allocation policy. Pure: declarations and leases in, decisions out.

Keeping every rule here, with no file or socket touching it, is what makes the
policy testable with plain values -- and it is the only module that decides who
gets which port.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from harbor_console.ports.declaration import Declaration, PortRequest
from harbor_console.ports.keys import addrs_overlap
from harbor_console.ports.ledger import Lease
from harbor_console.ports.live import LiveState

#: New grants come from here. Deliberately below the Linux ephemeral range
#: (32768-60999) so an allocated port cannot lose a race to an outbound socket.
BAND_START = 8100
BAND_END = 8999


class BandExhausted(Exception):
    """No free port remains in the allocation band."""


@dataclass(frozen=True)
class Decision:
    """What should happen to one declared port."""

    project: str
    port_name: str
    action: str  # "keep" | "grant" | "reassign"
    host: str
    addr: str
    port: int
    reason: str
    incumbent: Lease | None = None


def decide(
    declarations: Sequence[Declaration],
    leases: Sequence[Lease],
    live: LiveState,
    today: date,
) -> list[Decision]:
    """Resolve every declared port against the ledger and live host state."""
    held = list(leases)
    taken: list[tuple[str, str, int]] = []
    decisions: list[Decision] = []

    for declaration in declarations:
        for request in declaration.ports:
            decision = _decide_one(declaration, request, held, taken, live, today)
            decisions.append(decision)
            taken.append((decision.host, decision.addr, decision.port))

    return decisions


def _decide_one(
    declaration: Declaration,
    request: PortRequest,
    leases: Sequence[Lease],
    taken: Sequence[tuple[str, str, int]],
    live: LiveState,
    today: date,
) -> Decision:
    host, addr = declaration.host, request.addr

    def make(action: str, port: int, reason: str, incumbent: Lease | None = None) -> Decision:
        return Decision(
            project=declaration.project,
            port_name=request.name,
            action=action,
            host=host,
            addr=addr,
            port=port,
            reason=reason,
            incumbent=incumbent,
        )

    own = _lease_for(leases, declaration.project, request.name)

    # 1. A lease this project already holds is never disturbed. When the
    #    declaration does not yet record it, the ledger wins and we write it
    #    back -- the ledger is the authority on what is reserved.
    if own is not None:
        if own.port == request.assigned:
            return make("keep", own.port, "already leased")
        return make("grant", own.port, "ledger holds")

    # 2. A free preference is granted as asked.
    if request.want is not None:
        incumbent = _holder(leases, host, addr, request.want, exclude=declaration.project)
        if incumbent is None and _is_free(request.want, host, addr, leases, taken, live):
            return make("grant", request.want, "preference free")

        # 2b. Grandfathering: already running under this project's own container.
        if incumbent is None and _is_own_listener(live, request):
            return make("grant", request.want, "grandfathered: already running")

        if incumbent is not None:
            port = _first_free(host, addr, leases, taken, live)
            action = "reassign" if request.assigned is not None else "grant"
            return make(action, port, f"{request.want} held by {incumbent.project}", incumbent)

    # 3. No preference, or the preference was unavailable: next free in the band.
    port = _first_free(host, addr, leases, taken, live)
    return make("grant", port, "allocated from band")


def _lease_for(leases: Sequence[Lease], project: str, name: str) -> Lease | None:
    for lease in leases:
        if lease.project == project and lease.name == name:
            return lease
    return None


def _holder(
    leases: Sequence[Lease], host: str, addr: str, port: int, exclude: str
) -> Lease | None:
    """The other project leasing a contending (host, addr, port), if any."""
    candidates = [
        lease
        for lease in leases
        if lease.host == host
        and lease.port == port
        and addrs_overlap(lease.addr, addr)
        and lease.project != exclude
    ]
    return min(candidates, key=lambda lease: lease.granted) if candidates else None


def _is_own_listener(live: LiveState, request: PortRequest) -> bool:
    """True when this project's own container already listens on its preference."""
    if request.container is None or request.want is None:
        return False
    return live.container_on(request.want) == request.container


def _is_free(
    port: int,
    host: str,
    addr: str,
    leases: Sequence[Lease],
    taken: Sequence[tuple[str, str, int]],
    live: LiveState,
) -> bool:
    """Free means neither leased, nor listening, nor promised earlier this run."""
    for lease in leases:
        if lease.host == host and lease.port == port and addrs_overlap(lease.addr, addr):
            return False
    for taken_host, taken_addr, taken_port in taken:
        if taken_host == host and taken_port == port and addrs_overlap(taken_addr, addr):
            return False
    return not live.is_listening(addr, port)


def _first_free(
    host: str,
    addr: str,
    leases: Sequence[Lease],
    taken: Sequence[tuple[str, str, int]],
    live: LiveState,
) -> int:
    for port in range(BAND_START, BAND_END + 1):
        if _is_free(port, host, addr, leases, taken, live):
            return port
    raise BandExhausted(f"no free port in {BAND_START}-{BAND_END} on {host}")


def apply_decisions(
    leases: Sequence[Lease],
    decisions: Sequence[Decision],
    today: date,
) -> list[Lease]:
    """Fold decisions into the ledger, preserving the grant date of kept leases."""
    updated = list(leases)

    for decision in decisions:
        existing = _lease_for(updated, decision.project, decision.port_name)
        if existing is not None and decision.action == "keep":
            continue
        if existing is not None:
            updated.remove(existing)
        updated.append(
            Lease(
                project=decision.project,
                name=decision.port_name,
                host=decision.host,
                addr=decision.addr,
                port=decision.port,
                granted=existing.granted if existing and existing.port == decision.port else today,
            )
        )

    return updated
