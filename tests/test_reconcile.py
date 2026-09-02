from datetime import date

from harbor_console.docker import DOCKER_UNAVAILABLE, Container
from harbor_console.listening import Listener
from harbor_console.ports.ledger import Lease
from harbor_console.reconcile import (
    DECLARED_NOT_RUNNING,
    PORT_MISMATCH,
    RUNNING_NOT_DECLARED,
    find_drift,
)

GRANTED = date(2026, 9, 1)
HOST = "hpz440"


def lease(project, port, addr="0.0.0.0", name="web", host=HOST):
    return Lease(project, name, host, addr, port, GRANTED)


def kinds(drift):
    return [item.kind for item in drift]


def test_a_lease_with_nothing_listening_is_declared_not_running():
    drift = find_drift([lease("gte", 8080)], [], [], host=HOST)

    assert kinds(drift) == [DECLARED_NOT_RUNNING]
    assert "gte" in drift[0].detail


def test_a_lease_with_a_listener_is_no_drift():
    drift = find_drift(
        [lease("gte", 8080)],
        [Listener("0.0.0.0", 8080, None)],
        [Container("gte", (("0.0.0.0", 8080),))],
        host=HOST,
    )

    assert drift == ()


def test_a_wildcard_listener_satisfies_a_specific_address_lease():
    drift = find_drift(
        [lease("arm", 49152, addr="100.69.239.123")],
        [Listener("0.0.0.0", 49152, None)],
        [Container("arm", (("0.0.0.0", 49152),))],
        host=HOST,
    )

    assert drift == ()


def test_a_container_publishing_an_unleased_port_is_running_not_declared():
    drift = find_drift(
        [],
        [Listener("0.0.0.0", 9999, None)],
        [Container("stranger", (("0.0.0.0", 9999),))],
        host=HOST,
    )

    assert kinds(drift) == [RUNNING_NOT_DECLARED]
    assert "stranger" in drift[0].detail


def test_a_container_named_for_a_project_on_the_wrong_port_is_a_mismatch():
    drift = find_drift(
        [lease("gte", 8080)],
        [Listener("0.0.0.0", 9090, None)],
        [Container("gte", (("0.0.0.0", 9090),))],
        host=HOST,
    )

    assert kinds(drift) == [PORT_MISMATCH]
    assert "8080" in drift[0].detail
    assert "9090" in drift[0].detail


def test_a_sibling_container_honouring_the_lease_is_not_a_mismatch():
    """A multi-port project is ordinary: each lease may be served by its own container."""
    drift = find_drift(
        [lease("gte", 8080, name="web"), lease("gte", 9000, name="metrics")],
        [Listener("0.0.0.0", 8080, None), Listener("0.0.0.0", 9000, None)],
        [
            Container("gte", (("0.0.0.0", 8080),)),
            Container("gte-metrics", (("0.0.0.0", 9000),)),
        ],
        host=HOST,
    )

    assert kinds(drift) == []


def test_a_sidecar_that_is_down_is_declared_not_running_not_a_mismatch():
    """The named container never held the sidecar's port, so it has moved nothing."""
    drift = find_drift(
        [lease("gte", 8080, name="web"), lease("gte", 9000, name="metrics")],
        [Listener("0.0.0.0", 8080, None)],
        [Container("gte", (("0.0.0.0", 8080),))],
        host=HOST,
    )

    assert kinds(drift) == [DECLARED_NOT_RUNNING]
    assert "9000" in drift[0].detail


def test_two_projects_on_each_others_leased_ports_are_both_a_mismatch():
    """A swap is drift `.env` fallback produces in practice, and must be named.

    Another project's container may never cover this project's lease: if it
    could, a straight swap would answer every lease and show a clean page.
    """
    drift = find_drift(
        [lease("gte", 8080), lease("arm", 9090)],
        [Listener("0.0.0.0", 8080, None), Listener("0.0.0.0", 9090, None)],
        [
            Container("gte", (("0.0.0.0", 9090),)),
            Container("arm", (("0.0.0.0", 8080),)),
        ],
        host=HOST,
    )

    assert kinds(drift) == [PORT_MISMATCH, PORT_MISMATCH]
    assert "arm" in drift[0].detail
    assert "gte" in drift[1].detail


def test_a_three_way_rotation_names_every_project():
    """Nothing about the two-project case may depend on the cycle being short."""
    drift = find_drift(
        [lease("a", 1001), lease("b", 1002), lease("c", 1003)],
        [
            Listener("0.0.0.0", 1001, None),
            Listener("0.0.0.0", 1002, None),
            Listener("0.0.0.0", 1003, None),
        ],
        [
            Container("a", (("0.0.0.0", 1002),)),
            Container("b", (("0.0.0.0", 1003),)),
            Container("c", (("0.0.0.0", 1001),)),
        ],
        host=HOST,
    )

    assert kinds(drift) == [PORT_MISMATCH, PORT_MISMATCH, PORT_MISMATCH]
    assert [item.detail.split()[0] for item in drift] == ["a", "b", "c"]


def test_a_mismatch_does_not_mute_the_projects_other_dead_lease():
    """Mismatch is decided per lease, so it may only silence the lease it names.

    Here `gte` has moved off 8080, while its 9000 lease is covered by a
    stranger's container and has nothing listening. That second lease is the
    dangerous one: it must still be reported.
    """
    drift = find_drift(
        [lease("gte", 8080, name="web"), lease("gte", 9000, name="metrics")],
        [],
        [
            Container("gte", (("0.0.0.0", 7777),)),
            Container("other", (("0.0.0.0", 9000),)),
        ],
        host=HOST,
    )

    assert kinds(drift) == [PORT_MISMATCH, DECLARED_NOT_RUNNING]
    assert "8080" in drift[0].detail
    assert "9000" in drift[1].detail


def test_an_unmatched_name_reports_both_halves_instead_of_a_mismatch():
    """Names are matched exactly. Prefix or substring matching must fail here."""
    drift = find_drift(
        [lease("automatic-ripping-machine", 49152)],
        [Listener("0.0.0.0", 9999, None)],
        [Container("arm-rippers-dev", (("0.0.0.0", 9999),))],
        host=HOST,
    )

    assert kinds(drift) == [DECLARED_NOT_RUNNING, RUNNING_NOT_DECLARED]


def test_another_hosts_leases_are_neither_drift_nor_cover():
    drift = find_drift(
        [lease("gte", 8080), lease("elsewhere", 9999, host="nas")],
        [Listener("0.0.0.0", 8080, None)],
        [
            Container("gte", (("0.0.0.0", 8080),)),
            Container("squatter", (("0.0.0.0", 9999),)),
        ],
        host=HOST,
    )

    assert kinds(drift) == [RUNNING_NOT_DECLARED]
    assert "squatter" in drift[0].detail


def test_docker_unavailable_suppresses_the_container_side_only():
    drift = find_drift([lease("gte", 8080)], [], DOCKER_UNAVAILABLE, host=HOST)

    assert kinds(drift) == [DECLARED_NOT_RUNNING]


def test_docker_unavailable_never_claims_a_port_is_undeclared():
    drift = find_drift([], [Listener("0.0.0.0", 9999, None)], DOCKER_UNAVAILABLE, host=HOST)

    assert drift == ()


def test_a_container_publishing_nothing_is_not_drift():
    drift = find_drift([], [], [Container("shared-postgres", ())], host=HOST)

    assert drift == ()


def test_undeclared_ports_on_one_container_are_ordered_by_port():
    drift = find_drift(
        [],
        [],
        [Container("stranger", (("0.0.0.0", 9999), ("0.0.0.0", 9998)))],
        host=HOST,
    )

    assert kinds(drift) == [RUNNING_NOT_DECLARED, RUNNING_NOT_DECLARED]
    assert "0.0.0.0:9998" in drift[0].detail
    assert "0.0.0.0:9999" in drift[1].detail


def test_undeclared_ports_sharing_a_port_on_one_container_are_ordered_by_address():
    """The sort key is `(addr, port)`; a shared port leaves only the address."""
    drift = find_drift(
        [],
        [],
        [Container("stranger", (("10.0.0.2", 9999), ("10.0.0.1", 9999)))],
        host=HOST,
    )

    assert kinds(drift) == [RUNNING_NOT_DECLARED, RUNNING_NOT_DECLARED]
    assert "10.0.0.1:9999" in drift[0].detail
    assert "10.0.0.2:9999" in drift[1].detail


def test_leases_sharing_a_port_on_different_addresses_are_ordered_by_address():
    drift = find_drift(
        [lease("gte", 8080, addr="10.0.0.2"), lease("gte", 8080, addr="10.0.0.1")],
        [],
        [],
        host=HOST,
    )

    assert kinds(drift) == [DECLARED_NOT_RUNNING, DECLARED_NOT_RUNNING]
    assert "10.0.0.1:8080" in drift[0].detail
    assert "10.0.0.2:8080" in drift[1].detail


def test_findings_are_ordered_deterministically():
    """Order must not depend on input order. The wording is free to change."""
    leases = [lease("zeta", 8001), lease("alpha", 8002)]
    listeners = [Listener("0.0.0.0", 7000, None)]
    containers = [
        Container("stranger", (("0.0.0.0", 9999),)),
        Container("other", (("0.0.0.0", 9998),)),
    ]

    forward = find_drift(leases, listeners, containers, host=HOST)
    backward = find_drift(
        list(reversed(leases)),
        list(reversed(listeners)),
        list(reversed(containers)),
        host=HOST,
    )

    assert len(forward) == 4
    assert forward == backward
