from harbor_console.directory import (
    BYPASSES_PROXY,
    KIND_EDGE,
    KIND_HTTP,
    KIND_INTERNAL,
    KIND_TCP,
    ROUTE_ERROR,
    UNDECLARED_CONTAINER,
    UNDECLARED_TAILNET_LISTENER,
    Finding,
    Row,
    build_rows,
    declared_kind,
    find_findings,
    route_of,
)
from harbor_console.docker import DOCKER_UNAVAILABLE, Container
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


TAILNET = "100.69.239.123"


def test_a_clean_host_has_no_findings():
    containers = (
        http_container(),
        Container("ice-colder-mqtt", (("0.0.0.0", 1883),), {"harbor.kind": "tcp", "harbor.port": "1883"}),
        Container("gte-db-1", (), {"harbor.kind": "internal"}),
        Container("traefik", ((TAILNET, 80), (TAILNET, 443)), {"harbor.kind": "edge"}),
    )
    routers = (Router("parksmart@docker", HOST, "parksmart", True, None),)
    listeners = (Listener(TAILNET, 80, None), Listener(TAILNET, 443, None), Listener("0.0.0.0", 1883, None))

    assert find_findings(containers, routers, listeners, TAILNET) == ()


def test_an_unlabelled_container_is_undeclared():
    findings = find_findings((Container("mystery", ()),), (), (), TAILNET)

    assert findings == (Finding(UNDECLARED_CONTAINER, "container 'mystery' carries no traefik.enable or harbor.kind label"),)


def test_an_http_container_still_publishing_a_port_bypasses_the_proxy():
    container = http_container()
    container = Container(container.name, (("127.0.0.1", 8000),), container.labels, container.networks)

    findings = find_findings((container,), (Router("parksmart@docker", HOST, "parksmart", True, None),), (), TAILNET)

    assert findings == (Finding(BYPASSES_PROXY, "container 'parksmart-parksmart-1' publishes 127.0.0.1:8000, which no harbor.kind=tcp label accounts for"),)


def test_an_undeclared_container_with_a_port_reports_both():
    findings = find_findings((Container("mystery", (("0.0.0.0", 9000),)),), (), (), TAILNET)

    assert [f.kind for f in findings] == [UNDECLARED_CONTAINER, BYPASSES_PROXY]


def test_a_tcp_container_publishing_its_declared_port_is_fine():
    mqtt = Container("mqtt", (("0.0.0.0", 1883),), {"harbor.kind": "tcp", "harbor.port": "1883"})

    assert find_findings((mqtt,), (), (), TAILNET) == ()


def test_a_tcp_container_publishing_an_extra_port_bypasses():
    mqtt = Container("mqtt", (("0.0.0.0", 1883), ("0.0.0.0", 9001)), {"harbor.kind": "tcp", "harbor.port": "1883"})

    findings = find_findings((mqtt,), (), (), TAILNET)

    assert findings == (Finding(BYPASSES_PROXY, "container 'mqtt' publishes 0.0.0.0:9001, which no harbor.kind=tcp label accounts for"),)


def test_the_edge_may_publish_anything():
    edge = Container("traefik", ((TAILNET, 80), (TAILNET, 443), ("127.0.0.1", 8081)), {"harbor.kind": "edge"})

    assert find_findings((edge,), (), (), TAILNET) == ()


def test_a_disabled_router_is_a_route_error():
    routers = (Router("parksmart@docker", HOST, "parksmart", False, 'service "parksmart@docker" does not exist'),)

    findings = find_findings((http_container(),), routers, (), TAILNET)

    assert findings == (Finding(ROUTE_ERROR, 'router parksmart@docker is disabled: service "parksmart@docker" does not exist'),)


def test_an_http_container_traefik_does_not_know_is_a_route_error():
    findings = find_findings((http_container(),), (), (), TAILNET)

    assert findings == (Finding(ROUTE_ERROR, "container 'parksmart-parksmart-1' asks for router parksmart@docker, which Traefik does not report; is it on the harbor network?"),)


def test_route_errors_are_withheld_when_traefik_is_unavailable():
    assert find_findings((http_container(),), TRAEFIK_UNAVAILABLE, (), TAILNET) == ()


def test_container_findings_are_withheld_when_docker_is_unavailable():
    findings = find_findings(DOCKER_UNAVAILABLE, (), (Listener(TAILNET, 9999, None),), TAILNET)

    assert findings == ()


def test_a_host_process_on_the_tailnet_is_undeclared():
    findings = find_findings((), (), (Listener(TAILNET, 8443, None),), TAILNET)

    assert findings == (Finding(UNDECLARED_TAILNET_LISTENER, f"{TAILNET}:8443 is listening on the tailnet, and no container publishes it"),)


def test_a_tailnet_listener_a_container_publishes_is_not_reported():
    edge = Container("traefik", ((TAILNET, 443),), {"harbor.kind": "edge"})

    assert find_findings((edge,), (), (Listener(TAILNET, 443, None),), TAILNET) == ()


def test_a_wildcard_listener_is_not_a_tailnet_finding():
    assert find_findings((), (), (Listener("0.0.0.0", 22, None),), TAILNET) == ()


def test_an_ephemeral_tailnet_port_is_not_reported():
    assert find_findings((), (), (Listener(TAILNET, 53678, None),), TAILNET) == ()


def test_tailnet_findings_are_withheld_without_a_tailnet_address():
    assert find_findings((), (), (Listener(TAILNET, 8443, None),), None) == ()


def test_the_pages_own_port_is_not_an_undeclared_listener():
    findings = find_findings((), (), (Listener(TAILNET, 8100, None),), TAILNET, own_port=8100)

    assert findings == ()


def test_findings_come_in_a_stable_order():
    containers = (
        Container("mystery", (("0.0.0.0", 9000),)),
        http_container(),
    )
    listeners = (Listener(TAILNET, 8443, None),)

    kinds = [f.kind for f in find_findings(containers, (), listeners, TAILNET)]

    assert kinds == [UNDECLARED_CONTAINER, BYPASSES_PROXY, ROUTE_ERROR, UNDECLARED_TAILNET_LISTENER]
