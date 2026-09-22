import socket
from types import SimpleNamespace

import psutil

from harbor_console.listening import (
    ANY_ADDR,
    LISTENING_UNAVAILABLE,
    PROTO_TCP,
    PROTO_UDP,
    Listener,
    addrs_overlap,
    listening_sockets,
)


def conn(ip, port, status=psutil.CONN_LISTEN, pid=None, sock_type=socket.SOCK_STREAM, raddr=()):
    return SimpleNamespace(
        laddr=SimpleNamespace(ip=ip, port=port),
        status=status,
        pid=pid,
        type=sock_type,
        raddr=raddr,
    )


def udp(ip, port, pid=None, raddr=()):
    return conn(ip, port, status=psutil.CONN_NONE, pid=pid, sock_type=socket.SOCK_DGRAM, raddr=raddr)


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
    conns = [
        SimpleNamespace(laddr=(), status=psutil.CONN_LISTEN, pid=None, type=socket.SOCK_STREAM, raddr=())
    ]

    assert listening_sockets(net_connections=lambda kind: conns) == ()


def test_access_denied_degrades_to_unavailable():
    def denied(kind):
        raise psutil.AccessDenied()

    result = listening_sockets(net_connections=denied)

    assert result is LISTENING_UNAVAILABLE
    assert result == ()


def test_any_oserror_degrades_to_unavailable():
    def boom(kind):
        raise OSError("nope")

    assert listening_sockets(net_connections=boom) is LISTENING_UNAVAILABLE


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


def test_an_unexpected_exception_from_net_connections_degrades_to_unavailable():
    def boom(kind):
        raise RuntimeError("partly-readable /proc")

    assert listening_sockets(net_connections=boom) is LISTENING_UNAVAILABLE


def test_a_single_bad_connection_is_not_an_outage():
    # The per-connection except is not the whole-call except: one malformed
    # socket must still yield a real tuple, not the unavailable sentinel.
    bad = SimpleNamespace(laddr=(), status=psutil.CONN_LISTEN, pid=None, type=socket.SOCK_STREAM, raddr=())
    good = conn("0.0.0.0", 8080, pid=10)

    result = listening_sockets(net_connections=lambda kind: [bad, good])

    assert result is not LISTENING_UNAVAILABLE
    assert result == (Listener("0.0.0.0", 8080, 10),)


def test_wildcard_overlaps_everything():
    assert addrs_overlap(ANY_ADDR, "127.0.0.1")
    assert addrs_overlap("100.69.239.123", ANY_ADDR)


def test_same_specific_address_overlaps():
    assert addrs_overlap("127.0.0.1", "127.0.0.1")


def test_different_specific_addresses_do_not_overlap():
    assert not addrs_overlap("127.0.0.1", "100.69.239.123")


def test_the_collector_asks_for_both_protocols():
    seen = []

    def net_connections(kind):
        seen.append(kind)
        return []

    listening_sockets(net_connections=net_connections)

    assert seen == ["inet"]


def test_a_bound_udp_socket_is_collected():
    result = listening_sockets(net_connections=lambda kind: [udp("0.0.0.0", 41641, pid=7)])

    assert result == (Listener("0.0.0.0", 41641, 7, PROTO_UDP),)


def test_a_connected_udp_socket_is_not_collected():
    conns = [udp("192.168.86.26", 68, raddr=SimpleNamespace(ip="192.168.86.1", port=67))]

    assert listening_sockets(net_connections=lambda kind: conns) == ()


def test_a_udp_socket_does_not_need_the_listen_state():
    result = listening_sockets(net_connections=lambda kind: [udp("127.0.0.53", 53)])

    assert result[0].proto == PROTO_UDP


def test_tcp_still_requires_the_listen_state():
    conns = [conn("10.0.0.1", 51234, status=psutil.CONN_ESTABLISHED)]

    assert listening_sockets(net_connections=lambda kind: conns) == ()


def test_tcp_defaults_to_the_tcp_proto():
    result = listening_sockets(net_connections=lambda kind: [conn("0.0.0.0", 22)])

    assert result == (Listener("0.0.0.0", 22, None, PROTO_TCP),)


def test_the_same_port_on_both_protocols_is_two_listeners():
    conns = [udp("127.0.0.53", 53), conn("127.0.0.53", 53)]

    result = listening_sockets(net_connections=lambda kind: conns)

    assert [item.proto for item in result] == [PROTO_TCP, PROTO_UDP]


def test_the_ipv6_wildcard_is_normalised_for_udp_too():
    result = listening_sockets(net_connections=lambda kind: [udp("::", 41641)])

    assert result == (Listener("0.0.0.0", 41641, None, PROTO_UDP),)
