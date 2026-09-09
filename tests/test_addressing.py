from datetime import date

from harbor_console.addressing import probe_target, reachable_address
from harbor_console.ports.ledger import Lease

HOST = "hpz440"
TAILNET = "100.69.239.123"

WILDCARD = Lease("gte", "console", HOST, "0.0.0.0", 8080, date(2026, 9, 1))
ON_TAILNET = Lease("arm", "web", HOST, TAILNET, 49152, date(2026, 9, 1))
LOOPBACK = Lease("shared", "postgres", HOST, "127.0.0.1", 5432, date(2026, 9, 1))
LAN = Lease("thing", "web", HOST, "192.168.86.26", 7000, date(2026, 9, 1))
ELSEWHERE = Lease("other", "svc", "otherhost", "0.0.0.0", 9999, date(2026, 9, 1))


def test_a_wildcard_lease_is_reachable_at_the_tailnet_address():
    assert reachable_address(WILDCARD, HOST, TAILNET) == TAILNET


def test_a_lease_already_on_the_tailnet_address_keeps_it():
    assert reachable_address(ON_TAILNET, HOST, TAILNET) == TAILNET


def test_a_loopback_lease_is_not_moved_onto_the_tailnet():
    assert reachable_address(LOOPBACK, HOST, TAILNET) == "127.0.0.1"


def test_a_specific_non_tailnet_address_is_left_alone():
    assert reachable_address(LAN, HOST, TAILNET) == "192.168.86.26"


def test_a_lease_on_another_host_never_borrows_this_hosts_address():
    assert reachable_address(ELSEWHERE, HOST, TAILNET) == "0.0.0.0"


def test_an_unknown_tailnet_address_leaves_every_lease_alone():
    assert reachable_address(WILDCARD, HOST, None) == "0.0.0.0"


def test_the_probe_target_for_a_wildcard_lease_is_the_tailnet_address():
    """The defect this function exists to fix. Probing by hostname sends the
    request to the LAN address, where a tailnet-bound service is not listening
    -- so the page called a healthy service LISTENING instead of UP.
    """
    assert probe_target(WILDCARD, HOST, TAILNET) == TAILNET


def test_the_probe_target_for_a_tailnet_bound_lease_is_that_address():
    assert probe_target(ON_TAILNET, HOST, TAILNET) == TAILNET


def test_the_probe_target_for_a_loopback_lease_is_loopback():
    """Loopback is unreachable from the tailnet but perfectly reachable from
    this process, which runs on the same host. Probing it there is the only
    way to learn anything about it at all.
    """
    assert probe_target(LOOPBACK, HOST, TAILNET) == "127.0.0.1"


def test_the_probe_target_for_an_off_host_lease_is_its_hostname():
    """Another machine's address is not ours to guess; its name is the only
    handle this process has on it.
    """
    assert probe_target(ELSEWHERE, HOST, TAILNET) == "otherhost"


def test_the_probe_target_is_never_a_wildcard():
    """`0.0.0.0` is a bind, not a destination. With no tailnet address known
    there is nothing to substitute, so the probe falls back to the hostname it
    used before rather than connecting to a wildcard.
    """
    assert probe_target(WILDCARD, HOST, None) == HOST
