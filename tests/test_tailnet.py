import subprocess
from types import SimpleNamespace

import pytest

from harbor_console.tailnet import TailnetUnavailable, tailscale_address


def fake_run(stdout="", returncode=0, raises=None):
    def run(*_args, **_kwargs):
        if raises is not None:
            raise raises
        return SimpleNamespace(stdout=stdout, returncode=returncode)

    return run


def test_returns_the_first_address():
    assert tailscale_address(run=fake_run("100.69.239.123\n")) == "100.69.239.123"


def test_ignores_trailing_addresses():
    run = fake_run("100.69.239.123\nfd7a:115c:a1e0::1\n")

    assert tailscale_address(run=run) == "100.69.239.123"


def test_missing_binary_raises():
    with pytest.raises(TailnetUnavailable, match="tailscale"):
        tailscale_address(run=fake_run(raises=FileNotFoundError()))


def test_a_hanging_binary_raises_rather_than_blocking_startup():
    raises = subprocess.TimeoutExpired(cmd=["tailscale", "ip", "-4"], timeout=5.0)

    with pytest.raises(TailnetUnavailable, match="did not answer"):
        tailscale_address(run=fake_run(raises=raises))


def test_the_subprocess_is_given_a_timeout():
    seen = {}

    def run(*_args, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(stdout="100.69.239.123\n", returncode=0)

    tailscale_address(run=run, timeout=2.5)

    assert seen["timeout"] == 2.5


def test_non_zero_exit_raises():
    with pytest.raises(TailnetUnavailable):
        tailscale_address(run=fake_run(stdout="", returncode=1))


def test_empty_output_raises():
    with pytest.raises(TailnetUnavailable):
        tailscale_address(run=fake_run(stdout="\n"))


def test_unparseable_output_raises():
    with pytest.raises(TailnetUnavailable, match="not an IPv4"):
        tailscale_address(run=fake_run(stdout="something went wrong\n"))


def test_an_ipv6_only_answer_raises():
    with pytest.raises(TailnetUnavailable, match="not an IPv4"):
        tailscale_address(run=fake_run(stdout="fd7a:115c:a1e0::1\n"))
