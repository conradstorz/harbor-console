"""Guards on the deploy files: things a code test cannot catch because they
live in YAML, and that have already taken the edge down once."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TRAEFIK_COMPOSE = REPO / "deploy" / "traefik" / "compose.yaml"


def traefik_image_version() -> tuple[int, int]:
    text = TRAEFIK_COMPOSE.read_text(encoding="utf-8")
    match = re.search(r"^\s*image:\s*traefik:v(\d+)\.(\d+)\s*$", text, re.MULTILINE)
    assert match, "deploy/traefik/compose.yaml must pin `image: traefik:v<major>.<minor>`"
    return int(match.group(1)), int(match.group(2))


def traefik_command_flags() -> set[str]:
    text = TRAEFIK_COMPOSE.read_text(encoding="utf-8")
    return set(re.findall(r"^\s*-\s*(--\S+)\s*$", text, re.MULTILINE))


ENTRYPOINTS = ("web", "websecure", "traefik")
ENCODED_CHARACTERS = (
    "allowEncodedSlash",
    "allowEncodedBackSlash",
    "allowEncodedNullCharacter",
    "allowEncodedSemicolon",
    "allowEncodedPercent",
    "allowEncodedQuestionMark",
    "allowEncodedHash",
)


def test_every_entrypoint_drops_aliasing_header_names():
    # A header named X_Auth_User or X.Auth.User reaches a WSGI/CGI backend as
    # HTTP_X_AUTH_USER, the same variable the real X-Auth-User lands in, so a
    # client can spoof anything the proxy sets. Traefik v3.7.12 added
    # aliasHeadersStrategy and warns per entrypoint while it is unset.
    flags = traefik_command_flags()
    for entrypoint in ENTRYPOINTS:
        assert f"--entrypoints.{entrypoint}.http.aliasHeadersStrategy=delete" in flags


def test_every_entrypoint_rejects_encoded_path_characters():
    # CVE-2025-66490: Traefik and a backend that decode %2F, %00, %25 and
    # friends differently see two different paths for one request. Every
    # backend here is a small Python or Go app that never needs one encoded
    # in a path, so refuse them at the edge. Traefik warns at startup until
    # at least one entrypoint denies at least one of them.
    flags = traefik_command_flags()
    for entrypoint in ENTRYPOINTS:
        for option in ENCODED_CHARACTERS:
            assert f"--entrypoints.{entrypoint}.http.encodedCharacters.{option}=false" in flags


def test_traefik_image_negotiates_the_docker_api_version():
    # Docker Engine 29 refuses API versions below 1.40. Traefik v3.3 pinned
    # the Docker provider to API 1.24, so the 2026-10-08 engine upgrade on
    # hpz440 silently dropped every label-declared route while the file
    # route and the certificate kept working. Auto-negotiation landed in
    # v3.6.16 and v3.7.0; the pin must stay on a line that has it.
    assert traefik_image_version() >= (3, 7)


WEB_UNIT = REPO / "deploy" / "harbor-console-web.service"
INSTALL = REPO / "deploy" / "install.sh"


def test_the_web_unit_owns_the_verdict_directory():
    text = WEB_UNIT.read_text(encoding="utf-8")

    assert "RuntimeDirectory=harbor-console\n" in text
    assert "RuntimeDirectoryPreserve=yes\n" in text


def test_the_installer_ends_with_the_platform_checks():
    text = INSTALL.read_text(encoding="utf-8")

    assert ".venv/bin/harbor-console-check" in text
    assert text.index("harbor-console-check") > text.index("Bringing up hosted infrastructure")
    assert "rm -f /run/harbor-console/checks.json" in text
    assert text.index("rm -f /run/harbor-console/checks.json") < text.index(
        "systemctl restart harbor-console-web.service"
    )
