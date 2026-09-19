from harbor_console.directory import (
    KIND_EDGE,
    KIND_HTTP,
    KIND_INTERNAL,
    KIND_TCP,
    Row,
    build_rows,
    declared_kind,
    route_of,
)
from harbor_console.docker import Container
from harbor_console.listening import Listener
from harbor_console.probe import Health
from harbor_console.traefik import TRAEFIK_UNAVAILABLE, Router

HOST = "parksmart.hpz440.ohr3023.org"
UP = Health(True, "ok", "3 queued", (), None)
DOWN = Health(False, None, None, (), None)


def http_container(name="parksmart-parksmart-1", route="parksmart", host=HOST, **labels):
    return Container(
        name,
        (),
        {
            "traefik.enable": "true",
            f"traefik.http.routers.{route}.rule": f"Host(`{host}`)",
            **labels,
        },
        frozenset({"harbor"}),
    )


def test_declared_kind_reads_traefik_then_harbor_labels():
    assert declared_kind(http_container()) == KIND_HTTP
    assert declared_kind(Container("mqtt", (), {"harbor.kind": "tcp", "harbor.port": "1883"})) == KIND_TCP
    assert declared_kind(Container("db", (), {"harbor.kind": "internal"})) == KIND_INTERNAL
    assert declared_kind(Container("traefik", (), {"harbor.kind": "edge"})) == KIND_EDGE


def test_an_unknown_kind_is_undeclared():
    assert declared_kind(Container("x", (), {"harbor.kind": "banana"})) is None
    assert declared_kind(Container("x", ())) is None


def test_traefik_enable_false_is_not_http():
    assert declared_kind(Container("x", (), {"traefik.enable": "false"})) is None


def test_route_of_reads_the_router_label():
    assert route_of(http_container()) == ("parksmart", HOST)


def test_route_of_without_a_rule_falls_back_to_the_container_name():
    assert route_of(Container("plain", (), {"traefik.enable": "true"})) == ("plain", None)


def test_route_of_a_non_http_container_is_none():
    assert route_of(Container("db", (), {"harbor.kind": "internal"})) is None


def test_http_row_is_up_when_the_probe_answered():
    routers = (Router("parksmart@docker", HOST, "parksmart", True, None),)

    rows = build_rows((http_container(),), routers, (), {"parksmart": UP}, probed=True)

    assert rows == (
        Row("parksmart", KIND_HTTP, f"https://{HOST}/", "parksmart-parksmart-1", "", "UP"),
    )


def test_http_row_is_down_when_the_probe_failed():
    routers = (Router("parksmart@docker", HOST, "parksmart", True, None),)

    rows = build_rows((http_container(),), routers, (), {"parksmart": DOWN}, probed=True)

    assert rows[0].state == "DOWN"


def test_http_row_is_route_error_when_traefik_disabled_it():
    routers = (Router("parksmart@docker", HOST, "parksmart", False, "no service"),)

    rows = build_rows((http_container(),), routers, (), {"parksmart": DOWN}, probed=True)

    assert rows[0].state == "ROUTE ERROR"


def test_http_row_without_a_rule_is_route_error():
    rows = build_rows((Container("plain", (), {"traefik.enable": "true"}),), (), (), {}, probed=True)

    assert rows[0].state == "ROUTE ERROR"
    assert rows[0].target == ""


def test_http_row_is_unknown_before_the_first_probe():
    rows = build_rows((http_container(),), (), (), {}, probed=False)

    assert rows[0].state == "UNKNOWN"


def test_http_row_is_down_not_route_error_when_traefik_is_unavailable():
    rows = build_rows((http_container(),), TRAEFIK_UNAVAILABLE, (), {"parksmart": DOWN}, probed=True)

    assert rows[0].state == "DOWN"


def test_tcp_row_is_listening_when_the_port_is_held():
    mqtt = Container("ice-colder-mqtt", (("0.0.0.0", 1883),), {"harbor.kind": "tcp", "harbor.port": "1883"})

    rows = build_rows((mqtt,), (), (Listener("0.0.0.0", 1883, None),), {}, probed=True)

    assert rows == (Row("ice-colder-mqtt", KIND_TCP, "0.0.0.0:1883", "ice-colder-mqtt", "", "LISTENING"),)


def test_tcp_row_is_down_when_nothing_listens():
    mqtt = Container("ice-colder-mqtt", (("0.0.0.0", 1883),), {"harbor.kind": "tcp", "harbor.port": "1883"})

    rows = build_rows((mqtt,), (), (), {}, probed=True)

    assert rows[0].state == "DOWN"


def test_tcp_row_with_a_bad_port_label_is_route_error():
    mqtt = Container("mqtt", (), {"harbor.kind": "tcp", "harbor.port": "lots"})

    assert build_rows((mqtt,), (), (), {}, probed=True)[0].state == "ROUTE ERROR"


def test_tcp_row_whose_container_does_not_publish_the_port_is_route_error():
    mqtt = Container("mqtt", (("0.0.0.0", 9001),), {"harbor.kind": "tcp", "harbor.port": "1883"})

    rows = build_rows((mqtt,), (), (Listener("0.0.0.0", 1883, None),), {}, probed=True)

    assert rows[0].state == "ROUTE ERROR"
    assert rows[0].target == ""


def test_traefik_enable_wins_over_a_harbor_kind_label():
    both = Container("x", (), {"traefik.enable": "true", "harbor.kind": "internal"})

    assert declared_kind(both) == KIND_HTTP


def test_internal_row():
    db = Container("gte-db-1", (), {"harbor.kind": "internal", "harbor.description": "postgres"})

    rows = build_rows((db,), (), (), {}, probed=True)

    assert rows == (Row("gte-db-1", KIND_INTERNAL, "", "gte-db-1", "postgres", "INTERNAL"),)


def test_edge_row_is_listening_when_both_ports_are_held():
    edge = Container("traefik", (("100.69.239.123", 80), ("100.69.239.123", 443)), {"harbor.kind": "edge"})
    listeners = (Listener("100.69.239.123", 80, None), Listener("100.69.239.123", 443, None))

    rows = build_rows((edge,), (), listeners, {}, probed=True)

    assert rows[0].kind == KIND_EDGE
    assert rows[0].target == "100.69.239.123:80, 100.69.239.123:443"
    assert rows[0].state == "LISTENING"


def test_edge_row_is_down_when_one_port_is_missing():
    edge = Container("traefik", (("100.69.239.123", 80), ("100.69.239.123", 443)), {"harbor.kind": "edge"})

    rows = build_rows((edge,), (), (Listener("100.69.239.123", 80, None),), {}, probed=True)

    assert rows[0].state == "DOWN"


def test_undeclared_containers_produce_no_row():
    assert build_rows((Container("mystery", ()),), (), (), {}, probed=True) == ()


def test_rows_sort_by_name():
    a = http_container("zz-1", route="zeta", host="zeta.hpz440.ohr3023.org")
    b = Container("aa-1", (), {"harbor.kind": "internal"})

    assert [r.name for r in build_rows((a, b), (), (), {}, probed=True)] == ["aa-1", "zeta"]
