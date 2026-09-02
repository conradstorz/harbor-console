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
