"""The allocation policy. Pure: declarations and leases in, decisions out.

Keeping every rule here, with no file or socket touching it, is what makes the
policy testable with plain values -- and it is the only module that decides who
gets which port.

A lease is identified by ``(project, name, host)`` and claims the key
``(host, addr, port)``. ``0.0.0.0`` claims every address on its host, two
different specific addresses do not contend, and two different hosts never
contend. Every rule below is stated in terms of that whole key -- never in
terms of the port number alone.

Contention is therefore always asked about a *lease*, never about a project:
one project legitimately holds several named ports on one host, and those
contend with each other exactly as two projects' leases would. Every
exclusion below is keyed on the lease identity ``(project, name, host)``
under consideration.
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


#: A lease's identity: ``(project, name, host)``. The unit that owns a claim,
#: and so the only thing a contention check may exclude from its own answer.
_Identity = tuple[str, str, str]


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


@dataclass(frozen=True)
class _Promise:
    """A key promised to a project earlier in the same ``decide()`` call.

    It carries its claimant so that a want blocked by a promise can be reported
    as the conflict it is, rather than falling through to the band as though
    nothing had stood in the way.
    """

    project: str
    port_name: str
    host: str
    addr: str
    port: int


def decide(
    declarations: Sequence[Declaration],
    leases: Sequence[Lease],
    live: LiveState,
    today: date,
) -> list[Decision]:
    """Resolve every declared port against the ledger and live host state."""
    taken: list[_Promise] = []
    decisions: list[Decision] = []

    for declaration in declarations:
        for request in declaration.ports:
            decision = _decide_one(declaration, request, leases, taken, live, today)
            decisions.append(decision)
            taken.append(
                _Promise(
                    project=decision.project,
                    port_name=decision.port_name,
                    host=decision.host,
                    addr=decision.addr,
                    port=decision.port,
                )
            )

    return decisions


def _decide_one(
    declaration: Declaration,
    request: PortRequest,
    leases: Sequence[Lease],
    taken: Sequence[_Promise],
    live: LiveState,
    today: date,
) -> Decision:
    host, addr = declaration.host, request.addr

    def make(
        action: str,
        port: int,
        reason: str,
        incumbent: Lease | None = None,
        bind: str | None = None,
    ) -> Decision:
        """Build the decision. ``bind`` overrides the *requested* addr.

        A request to widen an addr can be refused while the lease itself is
        kept; the decision then has to report the addr actually held, not
        the one that was asked for, or the ledger would record a change that
        was refused.
        """
        return Decision(
            project=declaration.project,
            port_name=request.name,
            action=action,
            host=host,
            addr=addr if bind is None else bind,
            port=port,
            reason=reason,
            incumbent=incumbent,
        )

    identity: _Identity = (declaration.project, request.name, host)
    own = _lease_for(leases, declaration.project, request.name, host)

    # 1. A lease this project already holds is never disturbed -- for as long as
    #    it stays uncontended. The declaration may have widened its addr since
    #    the lease was granted (127.0.0.1 -> 0.0.0.0), and the wider key can
    #    collide with somebody else's lease. Short-circuiting on the port number
    #    alone would write a self-contradictory ledger that the loader then
    #    refuses to read at all, so it is the *new* key that gets checked.
    if own is not None:
        holder = _holder(leases, host, addr, own.port, exclude=identity)
        promise = _promised_by(taken, host, addr, own.port, exclude=identity)
        blocker: Lease | _Promise | None = holder if holder is not None else promise
        if blocker is None:
            if own.port == request.assigned:
                return make("keep", own.port, "already leased")
            return make("grant", own.port, "ledger holds")

        # Contended. Seniority decides, and it is decided by the grant dates
        # of the two *leases* -- never by which declaration happened to be
        # read first, or the outcome would flip with the order of the tree
        # walk. A promise carries no grant date because it was made moments
        # ago in this run, which makes its claimant junior to any lease.
        if holder is None or own.granted <= holder.granted:
            # The senior is never moved. Neither may it take the contended
            # key: the contender need not be re-declared in this run at all,
            # and would then never vacate, leaving the key claimed twice. So
            # the widening is refused and the existing lease stands as it is.
            action = "keep" if own.port == request.assigned else "grant"
            reason = (
                f"addr change to {addr} blocked by {blocker.project}; "
                f"keeping {own.addr}:{own.port}"
            )
            return make(action, own.port, reason, bind=own.addr)

        # The junior moves, which is what falling through does.

    # 2. A free preference is granted as asked.
    if request.want is not None:
        incumbent = _holder(leases, host, addr, request.want, exclude=identity)
        promised = _promised_by(taken, host, addr, request.want, exclude=identity)

        if incumbent is None and promised is None:
            if _is_free(request.want, host, addr, leases, taken, live):
                return make("grant", request.want, "preference free")

            # 2b. Grandfathering: already running under this project's own
            #     container. It overrides *liveness* only -- "the port is
            #     busy, but it is busy being mine". It never overrides a
            #     lease or a promise made earlier in this run: both are
            #     tested above, and their absence is what leads here.
            if _is_own_listener(live, request, host):
                return make("grant", request.want, "grandfathered: already running")

        if incumbent is not None:
            port = _first_free(host, addr, leases, taken, live)
            action = "reassign" if request.assigned is not None else "grant"
            return make(action, port, f"{request.want} held by {incumbent.project}", incumbent)

        if promised is not None:
            # The same conflict, except the winner was decided moments ago in
            # this very run and so has no Lease to point at yet. It is reported
            # the same way, with the claimant named in the reason instead.
            port = _first_free(host, addr, leases, taken, live)
            action = "reassign" if request.assigned is not None else "grant"
            return make(action, port, f"{request.want} claimed this run by {promised.project}")

    # 3. No preference, or the preference was unavailable: next free in the band.
    port = _first_free(host, addr, leases, taken, live)
    return make("grant", port, "allocated from band")


def _lease_for(leases: Sequence[Lease], project: str, name: str, host: str) -> Lease | None:
    """The lease identified by ``(project, name, host)``.

    The host is part of the identity: one project may run the same named port on
    several machines, and each of those is a lease in its own right.
    """
    for lease in leases:
        if lease.project == project and lease.name == name and lease.host == host:
            return lease
    return None


def _holder(
    leases: Sequence[Lease], host: str, addr: str, port: int, exclude: _Identity
) -> Lease | None:
    """The other lease on a contending (host, addr, port), if any.

    ``exclude`` is the one lease being decided, keyed ``(project, name,
    host)``. Excluding the whole project instead would let a project widen
    one of its ports on top of another of its own, which the ledger reads as
    a port claimed twice and refuses -- and the tool would then not start on
    its next run.

    When several contend -- two specific addresses both overlapped by a widening
    request -- the earliest grant is the incumbent, because the incumbent is
    never the one that moves.
    """
    candidates = [
        lease
        for lease in leases
        if lease.host == host
        and lease.port == port
        and addrs_overlap(lease.addr, addr)
        and (lease.project, lease.name, lease.host) != exclude
    ]
    return min(candidates, key=lambda lease: lease.granted) if candidates else None


def _promised_by(
    taken: Sequence[_Promise], host: str, addr: str, port: int, exclude: _Identity
) -> _Promise | None:
    """The other lease promised a contending key earlier in this same run.

    ``exclude`` is the lease being decided, so that a project asking about
    its second named port is answered about its first one, rather than
    having its own earlier promise waved through as "us".
    """
    for promise in taken:
        if (
            promise.host == host
            and promise.port == port
            and addrs_overlap(promise.addr, addr)
            and (promise.project, promise.port_name, promise.host) != exclude
        ):
            return promise
    return None


def _is_own_listener(live: LiveState, request: PortRequest, host: str) -> bool:
    """True when this project's own container already listens on its preference."""
    if request.container is None or request.want is None:
        return False
    if live.host != host:
        return False
    return live.container_on(request.want) == request.container


def _is_free(
    port: int,
    host: str,
    addr: str,
    leases: Sequence[Lease],
    taken: Sequence[_Promise],
    live: LiveState,
) -> bool:
    """Free means neither leased, nor listening, nor promised earlier this run."""
    for lease in leases:
        if lease.host == host and lease.port == port and addrs_overlap(lease.addr, addr):
            return False
    for promise in taken:
        if promise.host == host and promise.port == port and addrs_overlap(promise.addr, addr):
            return False
    if live.host != host:
        # Evidence about one machine says nothing about the sockets on another.
        return True
    return not live.is_listening(addr, port)


def _first_free(
    host: str,
    addr: str,
    leases: Sequence[Lease],
    taken: Sequence[_Promise],
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
    """Fold decisions into the ledger, preserving the grant date of kept leases.

    Seniority belongs to a key, not to a port number: a lease whose host or addr
    changed is claiming something it did not hold before, and its tenure starts
    today. Conversely a decision that changed only the addr still has to reach
    the ledger, so "keep" is a no-op only when the whole key is unchanged.
    """
    updated = list(leases)

    for decision in decisions:
        existing = _lease_for(updated, decision.project, decision.port_name, decision.host)
        same_key = existing is not None and (existing.host, existing.addr, existing.port) == (
            decision.host,
            decision.addr,
            decision.port,
        )
        if same_key and decision.action == "keep":
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
                granted=existing.granted if same_key and existing is not None else today,
            )
        )

    return updated
