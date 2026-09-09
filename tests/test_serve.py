import json
import subprocess

from harbor_console.serve import Proxy, serve_proxies

#: What `tailscale serve status --json` answers on hpz440 today: one HTTPS
#: front on 443 proxying to portainer, another on 8443 proxying to the gte
#: console. The `https+insecure` scheme is real and is why the backend is
#: parsed rather than pattern-matched on `http://`.
REAL = json.dumps(
    {
        "TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}},
        "Web": {
            "hpz440.tail69149b.ts.net:443": {
                "Handlers": {"/": {"Proxy": "https+insecure://127.0.0.1:9443"}}
            },
            "hpz440.tail69149b.ts.net:8443": {
                "Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}
            },
        },
    }
)


class FakeResult:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def test_reads_every_front_and_its_backend():
    proxies = serve_proxies(run=lambda *a, **k: FakeResult(REAL))

    assert proxies == (
        Proxy(443, "/", "127.0.0.1", 9443),
        Proxy(8443, "/", "127.0.0.1", 8080),
    )


def test_a_named_path_is_kept():
    """A handler may sit on a path rather than the root, and the path is what
    tells an operator which of several backends behind one port they mean.
    """
    body = json.dumps(
        {
            "Web": {
                "host:8443": {
                    "Handlers": {
                        "/": {"Proxy": "http://127.0.0.1:8080"},
                        "/api": {"Proxy": "http://127.0.0.1:8000"},
                    }
                }
            }
        }
    )

    assert serve_proxies(run=lambda *a, **k: FakeResult(body)) == (
        Proxy(8443, "/", "127.0.0.1", 8080),
        Proxy(8443, "/api", "127.0.0.1", 8000),
    )


def test_localhost_is_normalised_to_the_loopback_address():
    """The ledger records addresses, never names. A backend written as
    `localhost` would otherwise never match a lease on `127.0.0.1`.
    """
    body = json.dumps(
        {"Web": {"host:8443": {"Handlers": {"/": {"Proxy": "http://localhost:8080"}}}}}
    )

    assert serve_proxies(run=lambda *a, **k: FakeResult(body)) == (
        Proxy(8443, "/", "127.0.0.1", 8080),
    )


def test_a_backend_with_no_port_is_skipped():
    """Without an explicit port there is no true port to name, which is the
    whole reason this collector exists. Reporting a guess would be worse than
    reporting nothing.
    """
    body = json.dumps(
        {"Web": {"host:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1"}}}}}
    )

    assert serve_proxies(run=lambda *a, **k: FakeResult(body)) == ()


def test_a_handler_that_is_not_a_proxy_is_skipped():
    """`serve` can also mount a file or a directory. Those front no port."""
    body = json.dumps(
        {"Web": {"host:8443": {"Handlers": {"/": {"Path": "/var/www/index.html"}}}}}
    )

    assert serve_proxies(run=lambda *a, **k: FakeResult(body)) == ()


def test_no_serve_configuration_is_not_an_error():
    """`tailscale serve status --json` answers `{}` when nothing is served."""
    assert serve_proxies(run=lambda *a, **k: FakeResult("{}")) == ()


def test_a_missing_tailscale_binary_degrades_quietly():
    def run(*_a, **_k):
        raise FileNotFoundError("tailscale")

    assert serve_proxies(run=run) == ()


def test_a_non_zero_exit_degrades_quietly():
    assert serve_proxies(run=lambda *a, **k: FakeResult("", returncode=1)) == ()


def test_unparseable_output_degrades_quietly():
    assert serve_proxies(run=lambda *a, **k: FakeResult("not json")) == ()


def test_a_hang_degrades_quietly():
    """This runs in the prober thread on every cycle, like `docker ps`: a
    wedged binary with no bound would freeze the last good snapshot in place
    while it is still served as current.
    """

    def run(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="tailscale", timeout=2.0)

    assert serve_proxies(run=run) == ()


def test_the_collector_is_bounded():
    seen = {}

    def run(*_a, **kwargs):
        seen.update(kwargs)
        return FakeResult("{}")

    serve_proxies(run=run)

    assert seen["timeout"] > 0
