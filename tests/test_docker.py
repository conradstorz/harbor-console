import subprocess
from types import SimpleNamespace

from harbor_console.docker import DOCKER_UNAVAILABLE, Container, running_containers


def fake_run(stdout="", returncode=0, raises=None):
    def run(*_args, **_kwargs):
        if raises is not None:
            raise raises
        return SimpleNamespace(stdout=stdout, returncode=returncode)

    return run


def test_parses_names_and_published_ports():
    out = "acme\t0.0.0.0:8080->8080/tcp\ndelta\t100.69.239.123:49152->8080/tcp\n"

    result = running_containers(run=fake_run(out))

    assert result == (
        Container("acme", (("0.0.0.0", 8080),)),
        Container("delta", (("100.69.239.123", 49152),)),
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


def test_a_hanging_daemon_reports_unavailable_rather_than_blocking_forever():
    """A wedged `docker ps` must not be treated as the last good answer.

    Without a timeout on the subprocess, a hung daemon blocks the prober
    thread indefinitely and the last snapshot keeps being served as current
    -- 200, probed=True, docker_available=True -- with an ever-staler
    `collected` timestamp, while the allocator grants against it as though it
    were fresh.
    """
    raises = subprocess.TimeoutExpired(cmd=["docker", "ps"], timeout=2.0)

    assert running_containers(run=fake_run(raises=raises)) is DOCKER_UNAVAILABLE


def test_the_subprocess_is_given_a_timeout():
    seen = {}

    def run(*_args, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(stdout="", returncode=0)

    running_containers(run=run, timeout=2.5)

    assert seen["timeout"] == 2.5


def test_non_zero_exit_reports_unavailable():
    assert running_containers(run=fake_run(returncode=1)) is DOCKER_UNAVAILABLE


def test_no_containers_is_empty_not_unavailable():
    result = running_containers(run=fake_run(""))

    assert result == ()
    assert result is not DOCKER_UNAVAILABLE


def test_a_malformed_line_is_skipped_not_fatal():
    result = running_containers(run=fake_run("acme\t0.0.0.0:notaport->8080/tcp\n"))

    assert result == (Container("acme", ()),)


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
