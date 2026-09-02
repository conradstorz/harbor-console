from types import SimpleNamespace

from harbor_console.docker import DOCKER_UNAVAILABLE, Container, running_containers


def fake_run(stdout="", returncode=0, raises=None):
    def run(*_args, **_kwargs):
        if raises is not None:
            raise raises
        return SimpleNamespace(stdout=stdout, returncode=returncode)

    return run


def test_parses_names_and_published_ports():
    out = "gte\t0.0.0.0:8080->8080/tcp\narm\t100.69.239.123:49152->8080/tcp\n"

    result = running_containers(run=fake_run(out))

    assert result == (
        Container("arm", (("100.69.239.123", 49152),)),
        Container("gte", (("0.0.0.0", 8080),)),
    )


def test_ipv6_wildcard_publish_is_normalised():
    result = running_containers(run=fake_run("web\t:::8080->8080/tcp\n"))

    assert result[0].published == (("0.0.0.0", 8080),)


def test_a_container_publishing_nothing_still_appears():
    result = running_containers(run=fake_run("shared-postgres\t\n"))

    assert result == (Container("shared-postgres", ()),)


def test_exposed_but_unpublished_ports_are_ignored():
    result = running_containers(run=fake_run("db\t5432/tcp\n"))

    assert result[0].published == ()


def test_several_published_ports_on_one_container():
    out = "app\t0.0.0.0:8501->8501/tcp, 0.0.0.0:8502->8502/tcp\n"

    result = running_containers(run=fake_run(out))

    assert result[0].published == (("0.0.0.0", 8501), ("0.0.0.0", 8502))


def test_missing_binary_reports_unavailable():
    assert running_containers(run=fake_run(raises=FileNotFoundError())) is DOCKER_UNAVAILABLE


def test_non_zero_exit_reports_unavailable():
    assert running_containers(run=fake_run(returncode=1)) is DOCKER_UNAVAILABLE


def test_no_containers_is_empty_not_unavailable():
    result = running_containers(run=fake_run(""))

    assert result == ()
    assert result is not DOCKER_UNAVAILABLE


def test_a_malformed_line_is_skipped_not_fatal():
    result = running_containers(run=fake_run("gte\t0.0.0.0:notaport->8080/tcp\n"))

    assert result == (Container("gte", ()),)


def test_a_bad_entry_among_valid_ones_is_dropped_not_fatal():
    out = "app\t0.0.0.0:8501->8501/tcp, 0.0.0.0:notaport->8502/tcp, 0.0.0.0:8503->8503/tcp\n"

    result = running_containers(run=fake_run(out))

    assert result[0].published == (("0.0.0.0", 8501), ("0.0.0.0", 8503))


def test_bracketed_ipv6_publish_loses_its_brackets():
    result = running_containers(run=fake_run("web\t[::1]:8080->8080/tcp\n"))

    assert result[0].published == (("::1", 8080),)


def test_bracketed_ipv6_wildcard_publish_is_normalised():
    result = running_containers(run=fake_run("web\t[::]:8080->8080/tcp\n"))

    assert result[0].published == (("0.0.0.0", 8080),)
