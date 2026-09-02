from types import SimpleNamespace

import psutil

from harbor_console.listening import Listener, listening_sockets


def conn(ip, port, status=psutil.CONN_LISTEN, pid=None):
    return SimpleNamespace(laddr=SimpleNamespace(ip=ip, port=port), status=status, pid=pid)


def test_returns_only_listening_sockets():
    conns = [
        conn("0.0.0.0", 8080, pid=10),
        conn("10.0.0.1", 51234, status=psutil.CONN_ESTABLISHED, pid=11),
    ]

    result = listening_sockets(net_connections=lambda kind: conns)

    assert result == (Listener("0.0.0.0", 8080, 10),)


def test_ipv6_wildcard_is_normalised_to_the_ipv4_wildcard():
    result = listening_sockets(net_connections=lambda kind: [conn("::", 22)])

    assert result == (Listener("0.0.0.0", 22, None),)


def test_other_ipv6_addresses_are_left_alone():
    result = listening_sockets(net_connections=lambda kind: [conn("fd7a::1", 8443)])

    assert result[0].addr == "fd7a::1"


def test_a_socket_with_no_local_address_is_skipped():
    conns = [SimpleNamespace(laddr=(), status=psutil.CONN_LISTEN, pid=None)]

    assert listening_sockets(net_connections=lambda kind: conns) == ()


def test_access_denied_degrades_to_empty():
    def denied(kind):
        raise psutil.AccessDenied()

    assert listening_sockets(net_connections=denied) == ()


def test_any_oserror_degrades_to_empty():
    def boom(kind):
        raise OSError("nope")

    assert listening_sockets(net_connections=boom) == ()


def test_results_are_sorted_and_deduplicated():
    conns = [conn("0.0.0.0", 9000), conn("0.0.0.0", 22), conn("0.0.0.0", 22)]

    result = listening_sockets(net_connections=lambda kind: conns)

    assert [item.port for item in result] == [22, 9000]


def test_a_connection_missing_an_expected_attribute_is_skipped():
    # No `status` attribute at all -- unlike a psutil connection, which always
    # has one. A single malformed entry must not take the good one with it.
    bad = SimpleNamespace(laddr=SimpleNamespace(ip="10.0.0.2", port=9000), pid=5)
    good = conn("0.0.0.0", 8080, pid=10)

    result = listening_sockets(net_connections=lambda kind: [bad, good])

    assert result == (Listener("0.0.0.0", 8080, 10),)


def test_a_laddr_that_is_a_plain_tuple_is_skipped():
    # psutil's laddr is a named `addr(ip, port)` tuple; a plain 2-tuple has no
    # `.ip` attribute. This should be skipped like any other malformed entry.
    bad = SimpleNamespace(laddr=("10.0.0.2", 9000), status=psutil.CONN_LISTEN, pid=5)
    good = conn("0.0.0.0", 8080, pid=10)

    result = listening_sockets(net_connections=lambda kind: [bad, good])

    assert result == (Listener("0.0.0.0", 8080, 10),)


def test_a_port_that_will_not_int_is_skipped():
    # laddr.port as a non-numeric string can't be int()'d. Skipped like any
    # other malformed entry; its well-formed neighbour still comes back.
    bad = SimpleNamespace(
        laddr=SimpleNamespace(ip="10.0.0.2", port="not-a-port"),
        status=psutil.CONN_LISTEN,
        pid=5,
    )
    good = conn("0.0.0.0", 8080, pid=10)

    result = listening_sockets(net_connections=lambda kind: [bad, good])

    assert result == (Listener("0.0.0.0", 8080, 10),)


def test_an_unexpected_exception_from_net_connections_degrades_to_empty():
    def boom(kind):
        raise RuntimeError("partly-readable /proc")

    assert listening_sockets(net_connections=boom) == ()
