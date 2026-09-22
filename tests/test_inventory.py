from harbor_console.docker import DOCKER_UNAVAILABLE, Container
from harbor_console.inventory import (
    OWN_NAME,
    REACH_ANY,
    REACH_LAN,
    REACH_LOOPBACK,
    REACH_TAILNET,
    UNKNOWN,
    Entry,
    build_inventory,
    reach_of,
)
from harbor_console.listening import Listener

TAILNET = "100.69.239.123"
TAILNET_V6 = "fd7a:115c:a1e0::7b37:ef7d"


def test_the_wildcard_reaches_the_lan_and_the_tailnet():
    assert reach_of("0.0.0.0", TAILNET) == REACH_ANY


def test_the_ipv6_wildcard_reaches_the_lan_and_the_tailnet():
    assert reach_of("::", TAILNET) == REACH_ANY


def test_the_tailnet_address_reaches_the_tailnet():
    assert reach_of(TAILNET, TAILNET) == REACH_TAILNET


def test_a_tailscale_ula_address_reaches_the_tailnet():
    assert reach_of(TAILNET_V6, TAILNET) == REACH_TAILNET


def test_loopback_reaches_only_itself():
    assert reach_of("127.0.0.1", TAILNET) == REACH_LOOPBACK
    assert reach_of("127.0.0.53", TAILNET) == REACH_LOOPBACK
    assert reach_of("::1", TAILNET) == REACH_LOOPBACK


def test_another_address_reaches_the_lan():
    assert reach_of("192.168.86.26", TAILNET) == REACH_LAN


def test_a_link_local_address_reaches_the_lan():
    assert reach_of("fe80::a28c:fdff:fee8:3e59", TAILNET) == REACH_LAN


def test_an_unparseable_address_reaches_the_lan():
    assert reach_of("fe80::1%eno1", TAILNET) == REACH_LAN


def test_reachability_without_a_tailnet_address_still_classifies():
    assert reach_of("127.0.0.1", None) == REACH_LOOPBACK
    assert reach_of("192.168.86.26", None) == REACH_LAN


def test_a_listener_is_attributed_to_the_container_that_publishes_it():
    mqtt = Container("ice-colder-mqtt", (("0.0.0.0", 1883),), {"harbor.kind": "tcp"})

    result = build_inventory((Listener("0.0.0.0", 1883, None),), (mqtt,), TAILNET, 8100)

    assert result == (Entry("tcp", "0.0.0.0", 1883, REACH_ANY, "ice-colder-mqtt"),)


def test_attribution_overlaps_addresses_rather_than_matching_them():
    traefik = Container("traefik", ((TAILNET, 443),), {"harbor.kind": "edge"})

    result = build_inventory((Listener("0.0.0.0", 443, None),), (traefik,), TAILNET, 8100)

    assert result[0].accounted == "traefik"


def test_the_pages_own_bind_is_attributed_to_the_page():
    result = build_inventory((Listener(TAILNET, 8100, None),), (), TAILNET, 8100)

    assert result[0].accounted == OWN_NAME


def test_a_readable_pid_is_the_fallback_attribution():
    result = build_inventory((Listener("0.0.0.0", 22, 812),), (), TAILNET, 8100)

    assert result[0].accounted == "pid 812"


def test_a_listener_nothing_accounts_for_is_left_empty():
    result = build_inventory((Listener("0.0.0.0", 22, None),), (), TAILNET, 8100)

    assert result[0].accounted == ""


def test_attribution_is_unknown_when_docker_could_not_be_read():
    result = build_inventory(
        (Listener("0.0.0.0", 1883, None),), DOCKER_UNAVAILABLE, TAILNET, 8100
    )

    assert result[0].accounted == UNKNOWN


def test_a_readable_pid_still_beats_unknown_without_docker():
    result = build_inventory(
        (Listener("0.0.0.0", 22, 812),), DOCKER_UNAVAILABLE, TAILNET, 8100
    )

    assert result[0].accounted == "pid 812"


def test_the_pages_own_bind_is_known_without_docker():
    result = build_inventory(
        (Listener(TAILNET, 8100, None),), DOCKER_UNAVAILABLE, TAILNET, 8100
    )

    assert result[0].accounted == OWN_NAME


def test_udp_carries_its_protocol_through():
    result = build_inventory((Listener("0.0.0.0", 41641, None, "udp"),), (), TAILNET, 8100)

    assert result[0].proto == "udp"
    assert result[0].reach == REACH_ANY


def test_entries_are_ordered_by_port_then_address_then_protocol():
    listeners = (
        Listener("127.0.0.1", 8081, None),
        Listener("0.0.0.0", 53, None, "udp"),
        Listener("0.0.0.0", 53, None),
        Listener("0.0.0.0", 22, None),
    )

    result = build_inventory(listeners, (), TAILNET, 8100)

    assert [(e.port, e.proto) for e in result] == [
        (22, "tcp"),
        (53, "tcp"),
        (53, "udp"),
        (8081, "tcp"),
    ]
