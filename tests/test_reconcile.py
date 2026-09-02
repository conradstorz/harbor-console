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


def lease(project, port, addr="0.0.0.0", name="web"):
    return Lease(project, name, "hpz440", addr, port, GRANTED)


def kinds(drift):
    return [item.kind for item in drift]


def test_a_lease_with_nothing_listening_is_declared_not_running():
    drift = find_drift([lease("gte", 8080)], [], [], docker_available=True)

    assert kinds(drift) == [DECLARED_NOT_RUNNING]
    assert "gte" in drift[0].detail


def test_a_lease_with_a_listener_is_no_drift():
    drift = find_drift(
        [lease("gte", 8080)],
        [Listener("0.0.0.0", 8080, None)],
        [Container("gte", (("0.0.0.0", 8080),))],
        docker_available=True,
    )

    assert drift == ()


def test_a_wildcard_listener_satisfies_a_specific_address_lease():
    drift = find_drift(
        [lease("arm", 49152, addr="100.69.239.123")],
        [Listener("0.0.0.0", 49152, None)],
        [Container("arm", (("0.0.0.0", 49152),))],
        docker_available=True,
    )

    assert drift == ()


def test_a_container_publishing_an_unleased_port_is_running_not_declared():
    drift = find_drift(
        [],
        [Listener("0.0.0.0", 9999, None)],
        [Container("stranger", (("0.0.0.0", 9999),))],
        docker_available=True,
    )

    assert kinds(drift) == [RUNNING_NOT_DECLARED]
    assert "stranger" in drift[0].detail


def test_a_container_named_for_a_project_on_the_wrong_port_is_a_mismatch():
    drift = find_drift(
        [lease("gte", 8080)],
        [Listener("0.0.0.0", 9090, None)],
        [Container("gte", (("0.0.0.0", 9090),))],
        docker_available=True,
    )

    assert kinds(drift) == [PORT_MISMATCH]
    assert "8080" in drift[0].detail
    assert "9090" in drift[0].detail


def test_an_unmatched_name_reports_both_halves_instead_of_a_mismatch():
    drift = find_drift(
        [lease("automatic-ripping-machine", 49152)],
        [Listener("0.0.0.0", 49152, None)],
        [Container("arm-rippers-dev", (("0.0.0.0", 49152),))],
        docker_available=True,
    )

    assert kinds(drift) == []


def test_docker_unavailable_suppresses_the_container_side_only():
    drift = find_drift(
        [lease("gte", 8080)],
        [],
        DOCKER_UNAVAILABLE,
        docker_available=False,
    )

    assert kinds(drift) == [DECLARED_NOT_RUNNING]


def test_docker_unavailable_never_claims_a_port_is_undeclared():
    drift = find_drift(
        [],
        [Listener("0.0.0.0", 9999, None)],
        DOCKER_UNAVAILABLE,
        docker_available=False,
    )

    assert drift == ()


def test_a_container_publishing_nothing_is_not_drift():
    drift = find_drift([], [], [Container("shared-postgres", ())], docker_available=True)

    assert drift == ()


def test_findings_are_ordered_deterministically():
    drift = find_drift(
        [lease("zeta", 8001), lease("alpha", 8002)],
        [],
        [],
        docker_available=True,
    )

    assert [item.detail.split()[0] for item in drift] == ["alpha", "zeta"]
