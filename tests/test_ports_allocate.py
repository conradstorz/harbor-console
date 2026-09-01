from datetime import date
from pathlib import Path

import pytest

from harbor_console.ports.allocate import (
    BAND_END,
    BAND_START,
    BandExhausted,
    Decision,
    apply_decisions,
    decide,
)
from harbor_console.ports.declaration import Declaration, PortRequest
from harbor_console.ports.keys import addrs_overlap
from harbor_console.ports.ledger import Lease
from harbor_console.ports.live import Listener, LiveState

TODAY = date(2026, 9, 1)


def decl(
    project, name, want=None, assigned=None, container=None, addr="0.0.0.0", host="hpz440"
):
    return Declaration(
        project=project,
        host=host,
        path=Path(f"/tree/{project}/.harbor.toml"),
        ports=(
            PortRequest(
                name=name,
                want=want,
                assigned=assigned,
                addr=addr,
                container=container,
                health_path="/",
                hcstatus_path=None,
                description="",
            ),
        ),
    )


def live(*pairs, complete=True, host="hpz440"):
    return LiveState(
        host=host,
        listeners=tuple(
            Listener(addr=addr, port=port, container=container)
            for addr, port, container in pairs
        ),
        complete=complete,
    )


def test_free_want_is_granted():
    [decision] = decide([decl("p", "web", want=8080)], [], live(), TODAY)

    assert decision.action == "grant"
    assert decision.port == 8080


def test_existing_assignment_held_by_this_project_is_kept():
    leases = [Lease("p", "web", "hpz440", "0.0.0.0", 8090, date(2026, 8, 1))]

    [decision] = decide([decl("p", "web", want=8080, assigned=8090)], leases, live(), TODAY)

    assert decision.action == "keep"
    assert decision.port == 8090


def test_want_held_by_another_project_moves_the_newcomer_into_the_band():
    leases = [Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5))]

    [decision] = decide([decl("imageharbor", "dashboard", want=8080)], leases, live(), TODAY)

    assert decision.action == "grant"
    assert decision.port == BAND_START
    assert decision.incumbent is not None
    assert decision.incumbent.project == "gte"
    assert "gte/console" in decision.reason


def test_a_held_lease_is_written_back_when_the_declaration_lacks_it():
    leases = [Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5))]

    [decision] = decide([decl("gte", "console", want=8080)], leases, live(), TODAY)

    assert decision.action == "grant"
    assert decision.port == 8080
    assert decision.reason == "ledger holds"


def test_incumbent_is_never_moved():
    leases = [
        Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5)),
        Lease("imageharbor", "dashboard", "hpz440", "0.0.0.0", 8090, date(2026, 8, 9)),
    ]
    declarations = [
        decl("gte", "console", want=8080, assigned=8080),
        decl("imageharbor", "dashboard", want=8080, assigned=8090),
    ]

    decisions = decide(declarations, leases, live(), TODAY)

    assert [d.action for d in decisions] == ["keep", "keep"]


def test_a_listening_but_unleased_port_is_not_handed_out():
    state = live(("0.0.0.0", 8100, "somebody-else"))

    [decision] = decide([decl("p", "web")], [], state, TODAY)

    assert decision.port == BAND_START + 1


def test_a_leased_but_stopped_port_is_not_reclaimed():
    leases = [Lease("river", "web", "hpz440", "0.0.0.0", 8100, date(2026, 1, 1))]

    [decision] = decide([decl("p", "web")], leases, live(), TODAY)

    assert decision.port == BAND_START + 1


def test_grandfathering_grants_an_out_of_band_port_the_project_already_runs_on():
    state = live(("100.69.239.123", 49152, "arm-rippers-dev"))
    declaration = decl(
        "arm", "web", want=49152, container="arm-rippers-dev", addr="100.69.239.123"
    )

    [decision] = decide([declaration], [], state, TODAY)

    assert decision.action == "grant"
    assert decision.port == 49152
    assert "grandfathered" in decision.reason


def test_a_specific_address_does_not_collide_with_another_specific_address():
    leases = [Lease("arm", "web", "hpz440", "100.69.239.123", 8080, date(2026, 1, 1))]
    declaration = decl("p", "web", want=8080, addr="127.0.0.1")

    [decision] = decide([declaration], leases, live(), TODAY)

    assert decision.port == 8080


def test_a_declaration_with_no_ports_produces_no_decisions():
    empty = Declaration("shared-postgres", "hpz440", Path("/tree/x/.harbor.toml"), ())

    assert decide([empty], [], live(), TODAY) == []


def test_band_exhaustion_raises():
    leases = [
        Lease("p", f"n{port}", "hpz440", "0.0.0.0", port, date(2026, 1, 1))
        for port in range(BAND_START, BAND_END + 1)
    ]

    with pytest.raises(BandExhausted):
        decide([decl("new", "web")], leases, live(), TODAY)


def test_two_new_declarations_do_not_receive_the_same_port():
    decisions = decide([decl("a", "web"), decl("b", "web")], [], live(), TODAY)

    assert [d.port for d in decisions] == [BAND_START, BAND_START + 1]


def test_apply_decisions_records_grants_and_preserves_grant_dates():
    leases = [Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5))]
    decisions = decide([decl("imageharbor", "dashboard", want=8080)], leases, live(), TODAY)

    updated = apply_decisions(leases, decisions, TODAY)

    assert len(updated) == 2
    incumbent = next(lease for lease in updated if lease.project == "gte")
    newcomer = next(lease for lease in updated if lease.project == "imageharbor")
    assert incumbent.granted == date(2026, 7, 5)
    assert newcomer.granted == TODAY
    assert newcomer.port == BAND_START


def test_a_stale_assignment_against_a_held_port_is_reassigned():
    """The declaration claims 8080, but gte holds it and this project has no lease."""
    leases = [Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5))]

    [decision] = decide(
        [decl("imageharbor", "dashboard", want=8080, assigned=8080)], leases, live(), TODAY
    )

    assert decision.action == "reassign"
    assert decision.port == BAND_START
    assert decision.incumbent.project == "gte"


def test_apply_decisions_moves_a_project_off_its_previous_port():
    leases = [
        Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5)),
        Lease("p", "web", "hpz440", "0.0.0.0", 8200, date(2026, 8, 1)),
    ]
    decisions = [
        Decision("p", "web", "reassign", "hpz440", "0.0.0.0", 8300, "moved", None)
    ]

    updated = apply_decisions(leases, decisions, TODAY)

    ports = {(lease.project, lease.port) for lease in updated}
    assert ("p", 8200) not in ports
    assert ("p", 8300) in ports
    assert ("gte", 8080) in ports


def contending_pairs(leases):
    """Every pair of leases that claims the same (host, port) with overlapping addrs."""
    return [
        (a, b)
        for i, a in enumerate(leases)
        for b in leases[i + 1 :]
        if a.host == b.host and a.port == b.port and addrs_overlap(a.addr, b.addr)
    ]


def test_widening_an_addr_onto_a_held_port_moves_this_project_not_the_incumbent():
    """A held lease is short-circuited only while its *new* key contends with nobody.

    Granting 0.0.0.0:8080 here would sit on top of arm's 100.69.239.123:8080 and
    produce a ledger that `ledger.load_leases` refuses to read.
    """
    leases = [
        Lease("arm", "web", "hpz440", "100.69.239.123", 8080, date(2026, 1, 1)),
        Lease("p", "web", "hpz440", "127.0.0.1", 8080, date(2026, 2, 1)),
    ]
    declaration = decl("p", "web", want=8080, addr="0.0.0.0")

    [decision] = decide([declaration], leases, live(), TODAY)

    assert decision.port == BAND_START
    assert decision.incumbent is not None
    assert decision.incumbent.project == "arm"
    assert contending_pairs(apply_decisions(leases, [decision], TODAY)) == []


def test_a_lease_on_another_host_is_not_this_hosts_lease():
    """Lease identity is (project, name, host): hostA's number is not hostB's."""
    leases = [
        Lease("p", "web", "hostA", "0.0.0.0", 8111, date(2026, 1, 1)),
        Lease("p", "web", "hostB", "0.0.0.0", 8222, date(2026, 2, 1)),
    ]
    declaration = decl("p", "web", want=8222, assigned=8222, host="hostB")

    [decision] = decide([declaration], leases, live(host="hostB"), TODAY)

    assert decision.action == "keep"
    assert decision.host == "hostB"
    assert decision.port == 8222

    updated = apply_decisions(leases, [decision], TODAY)

    assert len(updated) == 2
    assert leases[0] in updated


def test_a_want_blocked_by_a_same_run_grant_is_reported_as_a_conflict():
    declarations = [
        decl("first", "web", want=8080, assigned=8080),
        decl("second", "web", want=8080, assigned=8080),
    ]

    winner, loser = decide(declarations, [], live(), TODAY)

    assert winner.port == 8080
    assert loser.action == "reassign"
    assert loser.port == BAND_START
    assert "first/web" in loser.reason
    assert loser.incumbent is None


def test_a_same_run_conflict_without_an_assignment_is_a_grant_not_a_reassign():
    declarations = [decl("first", "web", want=8080), decl("second", "web", want=8080)]

    _, loser = decide(declarations, [], live(), TODAY)

    assert loser.action == "grant"
    assert loser.port == BAND_START
    assert "first/web" in loser.reason


def test_live_state_for_another_host_does_not_block_a_port():
    """hpz440's listeners say nothing about what is free on another machine."""
    state = live(("0.0.0.0", BAND_START, "somebody-else"), host="hpz440")

    [decision] = decide([decl("p", "web", host="otherhost")], [], state, TODAY)

    assert decision.port == BAND_START


def test_the_earlier_granted_lease_is_the_incumbent():
    """The headline rule: seniority decides who stays, and the dates are what decide it."""

    def incumbent_of(gte_granted, river_granted):
        leases = [
            Lease("gte", "console", "hpz440", "100.69.239.123", 8080, gte_granted),
            Lease("river", "web", "hpz440", "127.0.0.1", 8080, river_granted),
        ]
        declaration = decl("imageharbor", "dashboard", want=8080, assigned=8080)
        [decision] = decide([declaration], leases, live(), TODAY)
        assert decision.incumbent is not None
        return decision.incumbent.project

    assert incumbent_of(date(2026, 1, 1), date(2026, 6, 1)) == "gte"
    assert incumbent_of(date(2026, 6, 1), date(2026, 1, 1)) == "river"


def test_apply_decisions_records_an_addr_change_on_an_unchanged_port():
    leases = [Lease("p", "web", "hpz440", "127.0.0.1", 8090, date(2026, 8, 1))]
    decisions = [
        Decision("p", "web", "keep", "hpz440", "0.0.0.0", 8090, "already leased", None)
    ]

    updated = apply_decisions(leases, decisions, TODAY)

    assert len(updated) == 1
    assert updated[0].addr == "0.0.0.0"
    assert updated[0].granted == TODAY


def test_widening_onto_a_port_held_under_this_projects_other_name_does_not_double_claim():
    """One project's two named ports contend with each other like anybody else's.

    gte holds 8100 twice over, on two addresses that do not overlap. Widening
    either one to 0.0.0.0 would sit on top of the other, and `load_leases` reads
    that as "port 8100 claimed twice" and refuses the whole ledger.
    """
    leases = [
        Lease("gte", "web", "hpz440", "127.0.0.1", 8100, date(2026, 1, 1)),
        Lease("gte", "api", "hpz440", "10.0.0.5", 8100, date(2026, 2, 1)),
    ]
    declaration = decl("gte", "api", want=8100, assigned=8100, addr="0.0.0.0")

    [decision] = decide([declaration], leases, live(), TODAY)

    assert decision.action == "reassign"
    # 8100 is BAND_START itself, and gte/web still holds it, so the band starts
    # handing out at the port after it.
    assert decision.port == BAND_START + 1
    assert decision.incumbent is not None
    assert decision.incumbent.name == "web"
    assert contending_pairs(apply_decisions(leases, [decision], TODAY)) == []


def test_grandfathering_does_not_override_a_lease_held_by_the_same_project():
    """Grandfathering answers liveness, not ownership.

    arm's own container listens on 49152, but arm's *api* lease already claims
    every address on that port. The running container is evidence about the
    socket, and says nothing about who holds the key.
    """
    leases = [Lease("arm", "api", "hpz440", "0.0.0.0", 49152, date(2026, 1, 1))]
    state = live(("100.69.239.123", 49152, "arm-dev"))
    declaration = decl(
        "arm", "web", want=49152, container="arm-dev", addr="100.69.239.123"
    )

    [decision] = decide([declaration], leases, state, TODAY)

    assert decision.port == BAND_START
    assert "grandfathered" not in decision.reason
    assert decision.incumbent is not None
    assert decision.incumbent.name == "api"
    assert contending_pairs(apply_decisions(leases, [decision], TODAY)) == []


def test_two_ports_of_one_project_are_not_both_grandfathered_onto_one_key():
    """A promise binds a project against itself, not only against strangers."""
    state = live(("100.69.239.123", 49152, "arm-dev"))
    declarations = [
        decl("arm", "web", want=49152, container="arm-dev", addr="100.69.239.123"),
        decl("arm", "api", want=49152, container="arm-dev", addr="100.69.239.123"),
    ]

    first, second = decide(declarations, [], state, TODAY)

    assert first.port == 49152
    assert "grandfathered" in first.reason
    assert second.port == BAND_START
    assert "arm/web" in second.reason
    assert contending_pairs(apply_decisions([], [first, second], TODAY)) == []


def test_grandfathering_does_not_grant_on_top_of_a_strangers_listener():
    """Ownership has to match the address as well as the port.

    arm-dev's own listener sits on 10.0.0.5, not on the requested 127.0.0.1 --
    that address is a stranger's socket. Waving the request through as
    "already running" would grant arm's declaration straight on top of it. The
    outcome must not depend on which listener happens to be probed first.
    """
    declaration = decl("arm", "web", want=49152, container="arm-dev", addr="127.0.0.1")

    for pairs in (
        [("10.0.0.5", 49152, "arm-dev"), ("127.0.0.1", 49152, "stranger")],
        [("127.0.0.1", 49152, "stranger"), ("10.0.0.5", 49152, "arm-dev")],
    ):
        state = live(*pairs)
        [decision] = decide([declaration], [], state, TODAY)

        assert decision.action == "grant"
        assert decision.reason == "allocated from band"
        assert decision.port == BAND_START


def test_grandfathering_still_grants_when_the_own_container_listens_on_the_addr():
    """The positive case: an address match still grandfathers.

    Same two listeners as above, but the declaration now asks for the address
    arm-dev's own container actually listens on. The liveness override must
    still fire -- the address check must not over-correct into never matching.
    """
    state = live(("10.0.0.5", 49152, "arm-dev"), ("127.0.0.1", 49152, "stranger"))
    declaration = decl("arm", "web", want=49152, container="arm-dev", addr="10.0.0.5")

    [decision] = decide([declaration], [], state, TODAY)

    assert decision.action == "grant"
    assert decision.port == 49152
    assert "grandfathered" in decision.reason


def test_when_two_contending_leaseholders_both_widen_only_the_junior_moves():
    """Seniority is settled by the grant dates, not by the order of the walk.

    Both leaseholders widen onto each other in one run. The earlier grant is
    never moved -- and it does not take the contended key either, because the
    junior might not have been re-declared at all, in which case nothing would
    have vacated it.
    """
    senior = Lease("senior", "web", "hpz440", "100.69.239.123", 8080, date(2026, 1, 1))
    junior = Lease("junior", "web", "hpz440", "127.0.0.1", 8080, date(2026, 2, 1))
    leases = [senior, junior]
    senior_decl = decl("senior", "web", want=8080, assigned=8080, addr="0.0.0.0")
    junior_decl = decl("junior", "web", want=8080, assigned=8080, addr="0.0.0.0")

    for order in ([senior_decl, junior_decl], [junior_decl, senior_decl]):
        decisions = {d.project: d for d in decide(order, leases, live(), TODAY)}

        kept = decisions["senior"]
        assert kept.action == "keep"
        assert (kept.addr, kept.port) == ("100.69.239.123", 8080)
        assert "junior/web" in kept.reason

        moved = decisions["junior"]
        assert moved.action == "reassign"
        assert (moved.addr, moved.port) == ("0.0.0.0", BAND_START)

        updated = apply_decisions(leases, list(decisions.values()), TODAY)
        assert contending_pairs(updated) == []
        assert senior in updated
