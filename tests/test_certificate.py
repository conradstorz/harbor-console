import socket
import ssl
from datetime import datetime, timezone

import pytest

from harbor_console.certificate import (
    Certificate,
    CertificateUnavailable,
    served_certificate,
)

PEER = {
    "subject": ((("commonName", "hpz440.ohr3023.org"),),),
    "subjectAltName": (("DNS", "hpz440.ohr3023.org"), ("DNS", "*.hpz440.ohr3023.org")),
    "notAfter": "Dec 18 12:05:54 2026 GMT",
}


def connector_returning(peer):
    seen = []

    def connect(address, port, server_name, timeout):
        seen.append((address, port, server_name, timeout))
        return peer

    return connect, seen


def test_reads_every_name_and_the_expiry():
    connect, _ = connector_returning(PEER)

    result = served_certificate("100.69.239.123", connector=connect)

    assert isinstance(result, Certificate)
    assert result.names == ("hpz440.ohr3023.org", "*.hpz440.ohr3023.org")
    assert result.not_after == datetime(2026, 12, 18, 12, 5, 54, tzinfo=timezone.utc)


def test_connects_to_the_address_with_the_route_hosts_sni():
    connect, seen = connector_returning(PEER)

    served_certificate("100.69.239.123", connector=connect)

    assert seen == [("100.69.239.123", 443, "harbor.hpz440.ohr3023.org", 2.0)]


def test_falls_back_to_the_subject_when_there_is_no_san():
    peer = {"subject": ((("commonName", "only.example"),),), "notAfter": PEER["notAfter"]}
    connect, _ = connector_returning(peer)

    result = served_certificate("100.69.239.123", connector=connect)

    assert isinstance(result, Certificate)
    assert result.names == ("only.example",)


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError("refused"),
        socket.timeout("timed out"),
        ssl.SSLCertVerificationError("certificate has expired"),
        ssl.SSLError("handshake failure"),
        OSError("network unreachable"),
    ],
)
def test_degrades_when_the_connection_fails(error):
    def connect(address, port, server_name, timeout):
        raise error

    result = served_certificate("100.69.239.123", connector=connect)

    assert isinstance(result, CertificateUnavailable)
    assert result.reason == f"{type(error).__name__}: {error}"


def test_degrades_on_an_empty_peer_certificate():
    connect, _ = connector_returning({})

    result = served_certificate("100.69.239.123", connector=connect)

    assert isinstance(result, CertificateUnavailable)
    assert "no certificate" in result.reason


def test_degrades_on_an_unparseable_expiry():
    connect, _ = connector_returning({**PEER, "notAfter": "someday"})

    result = served_certificate("100.69.239.123", connector=connect)

    assert isinstance(result, CertificateUnavailable)
