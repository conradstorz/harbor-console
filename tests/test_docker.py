import json
import subprocess
from types import SimpleNamespace

from harbor_console.docker import DOCKER_UNAVAILABLE, Container, running_containers


def inspect_entry(name, ports=None, labels=None, networks=("bridge",)):
    return {
        "Name": f"/{name}",
        "Config": {"Labels": labels or {}},
        "NetworkSettings": {
            "Ports": ports or {},
            "Networks": {net: {} for net in networks},
        },
    }


def fake_run(ids="", inspected=(), returncode=0, raises=None):
    """`docker ps -q` answers `ids`; `docker inspect` answers `inspected`."""
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if raises is not None:
            raise raises
        if args[:2] == ["docker", "ps"]:
            return SimpleNamespace(stdout=ids, returncode=returncode)
        return SimpleNamespace(stdout=json.dumps(list(inspected)), returncode=returncode)

    run.calls = calls
    return run


def test_parses_name_labels_ports_and_networks():
    entry = inspect_entry(
        "parksmart-parksmart-1",
        ports={"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]},
        labels={"traefik.enable": "true", "harbor.description": "parking"},
        networks=("harbor", "parksmart_default"),
    )

    result = running_containers(run=fake_run("abc\n", [entry]))

    assert result == (
        Container(
            "parksmart-parksmart-1",
            (("127.0.0.1", 8000),),
            {"traefik.enable": "true", "harbor.description": "parking"},
            frozenset({"harbor", "parksmart_default"}),
        ),
    )


def test_ipv6_wildcard_publish_is_normalised_and_deduplicated():
    entry = inspect_entry(
        "web",
        ports={
            "8080/tcp": [
                {"HostIp": "0.0.0.0", "HostPort": "8080"},
                {"HostIp": "::", "HostPort": "8080"},
            ]
        },
    )

    result = running_containers(run=fake_run("a\n", [entry]))

    assert result[0].published == (("0.0.0.0", 8080),)


def test_exposed_but_unpublished_ports_are_ignored():
    entry = inspect_entry("db", ports={"5432/tcp": None})

    result = running_containers(run=fake_run("a\n", [entry]))

    assert result[0].published == ()
    assert result[0].labels == {}


def test_udp_publishes_are_ignored():
    entry = inspect_entry("dns", ports={"53/udp": [{"HostIp": "0.0.0.0", "HostPort": "53"}]})

    assert running_containers(run=fake_run("a\n", [entry]))[0].published == ()


def test_containers_sort_by_name():
    entries = [inspect_entry("zeta"), inspect_entry("alpha")]

    result = running_containers(run=fake_run("a\nb\n", entries))

    assert [c.name for c in result] == ["alpha", "zeta"]


def test_no_running_containers_skips_inspect():
    run = fake_run("")

    assert running_containers(run=run) == ()
    assert len(run.calls) == 1


def test_missing_binary_reports_unavailable():
    assert running_containers(run=fake_run(raises=FileNotFoundError())) is DOCKER_UNAVAILABLE


def test_nonzero_exit_reports_unavailable():
    assert running_containers(run=fake_run("a\n", returncode=1)) is DOCKER_UNAVAILABLE


def test_a_hanging_daemon_reports_unavailable_rather_than_blocking_forever():
    raises = subprocess.TimeoutExpired(cmd=["docker"], timeout=2.0)

    assert running_containers(run=fake_run(raises=raises)) is DOCKER_UNAVAILABLE


def test_malformed_inspect_json_reports_unavailable():
    def run(args, **_kwargs):
        if args[:2] == ["docker", "ps"]:
            return SimpleNamespace(stdout="a\n", returncode=0)
        return SimpleNamespace(stdout="not json", returncode=0)

    assert running_containers(run=run) is DOCKER_UNAVAILABLE


def test_one_malformed_entry_is_skipped_not_fatal():
    entries = [{"Name": "/ok"}, inspect_entry("fine")]

    result = running_containers(run=fake_run("a\nb\n", entries))

    assert [c.name for c in result] == ["fine", "ok"]
    assert result[1].published == ()
