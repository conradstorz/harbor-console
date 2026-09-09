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
        Proxy(443, "/", "127.0.0.1", 9443, "hpz440.tail69149b.ts.net", "https"),
        Proxy(8443, "/", "127.0.0.1", 8080, "hpz440.tail69149b.ts.net", "https"),
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
        Proxy(8443, "/", "127.0.0.1", 8080, "host", "https"),
        Proxy(8443, "/api", "127.0.0.1", 8000, "host", "https"),
    )


def test_localhost_is_normalised_to_the_loopback_address():
    """The ledger records addresses, never names. A backend written as
    `localhost` would otherwise never match a lease on `127.0.0.1`.
    """
    body = json.dumps(
        {"Web": {"host:8443": {"Handlers": {"/": {"Proxy": "http://localhost:8080"}}}}}
    )

    assert serve_proxies(run=lambda *a, **k: FakeResult(body)) == (
        Proxy(8443, "/", "127.0.0.1", 8080, "host", "https"),
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


def test_the_front_host_and_scheme_are_kept():
    """The address a browser can actually use. `tailscale serve` terminates
    TLS for the MagicDNS name, and an app that sets Secure cookies -- GTE
    does -- will not complete a login over the plain leased port, so the
    front's own URL is the only one that works.
    """
    proxies = serve_proxies(run=lambda *a, **k: FakeResult(REAL))

    assert proxies[1].front_host == "hpz440.tail69149b.ts.net"
    assert proxies[1].scheme == "https"
    assert proxies[1].url == "https://hpz440.tail69149b.ts.net:8443/"


def test_the_default_https_port_is_left_off_the_url():
    proxies = serve_proxies(run=lambda *a, **k: FakeResult(REAL))

    assert proxies[0].url == "https://hpz440.tail69149b.ts.net/"


def test_a_named_path_is_part_of_the_url():
    body = json.dumps(
        {
            "TCP": {"8443": {"HTTPS": True}},
            "Web": {
                "host.ts.net:8443": {"Handlers": {"/api": {"Proxy": "http://127.0.0.1:8000"}}}
            },
        }
    )

    assert serve_proxies(run=lambda *a, **k: FakeResult(body))[0].url == (
        "https://host.ts.net:8443/api"
    )


def test_a_plain_http_front_is_not_called_https():
    """`tailscale serve --http` fronts without TLS. Handing a reader an
    https:// URL for it produces a connection error, not a page.
    """
    body = json.dumps(
        {
            "TCP": {"8080": {"HTTP": True}},
            "Web": {
                "host.ts.net:8080": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9000"}}}
            },
        }
    )

    proxy = serve_proxies(run=lambda *a, **k: FakeResult(body))[0]
    assert proxy.scheme == "http"
    assert proxy.url == "http://host.ts.net:8080/"


def test_an_unstated_front_is_assumed_https():
    """`serve` is HTTPS unless told otherwise, and the TCP map may be absent
    on an older daemon. The common case is the safe default here: an https://
    URL to a plain front fails loudly, where http:// to a TLS front could be
    redirected or silently downgraded.
    """
    body = json.dumps(
        {"Web": {"host.ts.net:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}}}}
    )

    assert serve_proxies(run=lambda *a, **k: FakeResult(body))[0].scheme == "https"
