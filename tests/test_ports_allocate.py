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
from harbor_console.ports.ledger import Lease
from harbor_console.ports.live import Listener, LiveState

TODAY = date(2026, 9, 1)


def decl(project, name, want=None, assigned=None, container=None, addr="0.0.0.0"):
    return Declaration(
        project=project,
        host="hpz440",
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


def live(*pairs, complete=True):
    return LiveState(
        host="hpz440",
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
