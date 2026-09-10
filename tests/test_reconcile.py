from datetime import date

from harbor_console.docker import DOCKER_UNAVAILABLE, Container
from harbor_console.listening import Listener
from harbor_console.ports.ledger import Lease
from harbor_console.reconcile import (
    DECLARED_NOT_RUNNING,
    PORT_MISMATCH,
    RUNNING_NOT_DECLARED,
    UNDECLARED_TAILNET_LISTENER,
    find_drift,
)
from harbor_console.serve import Proxy

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


def test_a_named_swap_is_still_a_mismatch_when_the_other_lease_is_on_another_host():
    """A container named for a fleet project is not a sidecar just because its
    own lease lives elsewhere.

    `arm` holds no lease on this host, so it generates no finding of its own
    here -- but its name still names a fleet project. Treating it as an
    anonymous sidecar because its lease is out of scope lets it cover `gte`'s
    lease and swallow the swap `gte` made with it.
    """
    drift = find_drift(
        [lease("gte", 8080), lease("arm", 8080, host="other")],
        [Listener("0.0.0.0", 8080, None), Listener("0.0.0.0", 9090, None)],
        [
            Container("gte", (("0.0.0.0", 9090),)),
            Container("arm", (("0.0.0.0", 8080),)),
        ],
        host=HOST,
    )

    assert kinds(drift) == [PORT_MISMATCH]
    assert "gte" in drift[0].detail
    assert "8080" in drift[0].detail
    assert "9090" in drift[0].detail


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


TAILNET = "100.69.239.123"


def test_an_undeclared_tailnet_listener_is_reported():
    """The gap that let `tailscale serve` hold 8443 invisibly for weeks.

    `running-not-declared` walks containers, so a *host process* binding a
    tailnet port produced no finding at all: no lease to list it in the
    directory, and no container to catch it in drift. Binding the tailnet
    address specifically is a deliberate act of publishing to the tailnet,
    which is exactly what the ledger governs.
    """
    drift = find_drift(
        [],
        [Listener(TAILNET, 8443, None)],
        [],
        host=HOST,
        tailnet_address=TAILNET,
    )

    assert kinds(drift) == [UNDECLARED_TAILNET_LISTENER]
    assert f"{TAILNET}:8443" in drift[0].detail


def test_a_wildcard_listener_is_not_an_undeclared_tailnet_listener():
    """`0.0.0.0` is every daemon on the box -- sshd, resolved, a dev server.
    Flagging those would bury the finding this rule exists to make in noise
    the ledger was never meant to govern.
    """
    drift = find_drift([], [Listener("0.0.0.0", 22, None)], [], host=HOST, tailnet_address=TAILNET)

    assert kinds(drift) == []


def test_a_loopback_listener_is_not_an_undeclared_tailnet_listener():
    drift = find_drift(
        [], [Listener("127.0.0.1", 9443, None)], [], host=HOST, tailnet_address=TAILNET
    )

    assert kinds(drift) == []


def test_a_leased_tailnet_listener_is_not_undeclared():
    drift = find_drift(
        [lease("arm", 49152, addr=TAILNET)],
        [Listener(TAILNET, 49152, None)],
        [Container("arm", ((TAILNET, 49152),))],
        host=HOST,
        tailnet_address=TAILNET,
    )

    assert kinds(drift) == []


def test_a_container_on_a_tailnet_port_is_reported_once_as_a_container():
    """`imageharbor` publishes 100.69.239.123:8087 and holds no lease. It is
    already `running-not-declared`; reporting it again as an undeclared
    listener would print the same port twice under two names.
    """
    drift = find_drift(
        [],
        [Listener(TAILNET, 8087, None)],
        [Container("imageharbor-imageharbor-1", ((TAILNET, 8087),))],
        host=HOST,
        tailnet_address=TAILNET,
    )

    assert kinds(drift) == [RUNNING_NOT_DECLARED]


def test_nothing_is_called_undeclared_when_docker_could_not_be_read():
    """Without container evidence there is no way to know the listener is not
    a container, which is the same reason `running-not-declared` is withheld.
    """
    drift = find_drift(
        [], [Listener(TAILNET, 8443, None)], DOCKER_UNAVAILABLE, host=HOST, tailnet_address=TAILNET
    )

    assert kinds(drift) == []


def test_nothing_is_called_undeclared_without_a_tailnet_address():
    """With no address known there is no way to tell a tailnet bind from any
    other specific address, so the rule does not run.
    """
    drift = find_drift([], [Listener(TAILNET, 8443, None)], [], host=HOST)

    assert kinds(drift) == []


def test_an_undeclared_listener_names_the_proxy_and_the_lease_behind_it():
    """The finding that makes 8443 actionable: it fronts the gte console.

    A second backend on the same front that no lease covers keeps the port
    from being fully accounted for, so the finding still stands and the
    leased backend's clause is still exercised.
    """
    drift = find_drift(
        [lease("gte", 8080, name="console")],
        [Listener(TAILNET, 8443, None), Listener("0.0.0.0", 8080, None)],
        [Container("gte", (("0.0.0.0", 8080),))],
        host=HOST,
        tailnet_address=TAILNET,
        proxies=(
            Proxy(8443, "/", "127.0.0.1", 8080),
            Proxy(8443, "/other", "127.0.0.1", 9999),
        ),
    )

    assert kinds(drift) == [UNDECLARED_TAILNET_LISTENER]
    detail = drift[0].detail
    assert "tailscale serve" in detail
    assert "127.0.0.1:8080" in detail
    assert "gte" in detail
    assert "console" in detail


def test_an_undeclared_listener_names_the_container_behind_the_proxy():
    """No lease covers portainer's 9443, but a container does -- which is
    still the answer to "what code is running there".
    """
    drift = find_drift(
        [],
        [Listener(TAILNET, 443, None), Listener("127.0.0.1", 9443, None)],
        [Container("portainer", (("127.0.0.1", 9443),))],
        host=HOST,
        tailnet_address=TAILNET,
        proxies=(Proxy(443, "/", "127.0.0.1", 9443),),
    )

    undeclared = [item for item in drift if item.kind == UNDECLARED_TAILNET_LISTENER]
    assert len(undeclared) == 1
    assert "127.0.0.1:9443" in undeclared[0].detail
    assert "portainer" in undeclared[0].detail


def test_an_undeclared_listener_says_when_nothing_declares_the_backend():
    drift = find_drift(
        [],
        [Listener(TAILNET, 8443, None)],
        [],
        host=HOST,
        tailnet_address=TAILNET,
        proxies=(Proxy(8443, "/", "127.0.0.1", 5000),),
    )

    assert "127.0.0.1:5000" in drift[0].detail
    assert "nothing declares" in drift[0].detail


def test_an_undeclared_listener_claims_no_proxy_it_did_not_see():
    """`serve_proxies` degrades to an empty tuple when tailscale could not be
    asked. Saying "nothing is proxying it" on that evidence would be a false
    claim in exactly the case the collector failed, so the finding says
    nothing about proxying at all.
    """
    drift = find_drift(
        [], [Listener(TAILNET, 8443, None)], [], host=HOST, tailnet_address=TAILNET
    )

    assert "proxy" not in drift[0].detail.lower()


def test_a_named_proxy_path_is_reported():
    drift = find_drift(
        [],
        [Listener(TAILNET, 8443, None)],
        [],
        host=HOST,
        tailnet_address=TAILNET,
        proxies=(Proxy(8443, "/api", "127.0.0.1", 8000),),
    )

    assert "/api" in drift[0].detail


def test_every_backend_behind_one_front_is_named():
    drift = find_drift(
        [],
        [Listener(TAILNET, 8443, None)],
        [],
        host=HOST,
        tailnet_address=TAILNET,
        proxies=(
            Proxy(8443, "/", "127.0.0.1", 8080),
            Proxy(8443, "/api", "127.0.0.1", 8000),
            Proxy(443, "/", "127.0.0.1", 9443),
        ),
    )

    assert "127.0.0.1:8080" in drift[0].detail
    assert "127.0.0.1:8000" in drift[0].detail
    assert "9443" not in drift[0].detail


def test_a_kernel_assigned_ephemeral_port_is_not_reported():
    """Real noise, found the first time this rule ran against hpz440:
    tailscaled's peerapi binds a random high port on the tailnet address, and
    it is a different port after every restart.

    A port nobody chose is not a port anybody published -- nothing can be
    pointed at an address that moves on reboot -- so it is not the deliberate
    act this rule exists to catch. A standing false finding that changes shape
    daily teaches operators to ignore the drift section, which costs more than
    the rule is worth.

    Leases inside the range are unaffected: ARM holds 49152, and a covered
    listener never reaches this test.
    """
    drift = find_drift(
        [], [Listener(TAILNET, 53678, None)], [], host=HOST, tailnet_address=TAILNET
    )

    assert kinds(drift) == []


def test_a_lease_inside_the_ephemeral_range_is_still_reconciled():
    """The range is not a blind spot, only a silence about *undeclared*
    listeners. ARM's 49152 lease still reports when nothing holds it.
    """
    drift = find_drift(
        [lease("automatic-ripping-machine", 49152, addr=TAILNET)],
        [],
        [],
        host=HOST,
        tailnet_address=TAILNET,
    )

    assert kinds(drift) == [DECLARED_NOT_RUNNING]


def test_a_proxied_ephemeral_port_is_still_reported():
    """A `tailscale serve` front is deliberate wherever it lands: somebody
    configured it, and it survives a restart. The evidence of intent
    outweighs the port's range.
    """
    drift = find_drift(
        [],
        [Listener(TAILNET, 41000, None)],
        [],
        host=HOST,
        tailnet_address=TAILNET,
        proxies=(Proxy(41000, "/", "127.0.0.1", 8080),),
    )

    assert kinds(drift) == [UNDECLARED_TAILNET_LISTENER]


def test_a_serve_front_whose_backend_is_leased_is_not_drift():
    tailnet = "100.69.239.123"
    leases = [Lease("gte", "console", "hpz440", "0.0.0.0", 8080, date(2026, 9, 1))]
    listeners = [
        Listener("0.0.0.0", 8080, None),
        Listener(tailnet, 8443, None),  # tailscaled's front
    ]
    containers = (Container("gte", (("0.0.0.0", 8080),)),)
    proxies = (Proxy(8443, "/", "127.0.0.1", 8080),)

    findings = find_drift(
        leases, listeners, containers, "hpz440",
        tailnet_address=tailnet, proxies=proxies,
    )

    assert findings == ()


def test_a_serve_front_to_an_unleased_backend_is_still_reported():
    tailnet = "100.69.239.123"
    listeners = [Listener(tailnet, 8443, None)]
    proxies = (Proxy(8443, "/", "127.0.0.1", 9999),)

    findings = find_drift(
        [], listeners, (), "hpz440", tailnet_address=tailnet, proxies=proxies,
    )

    assert len(findings) == 1
    assert findings[0].kind == "undeclared-tailnet-listener"
    assert "nothing declares" in findings[0].detail
