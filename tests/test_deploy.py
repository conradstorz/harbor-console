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


def test_traefik_image_negotiates_the_docker_api_version():
    # Docker Engine 29 refuses API versions below 1.40. Traefik v3.3 pinned
    # the Docker provider to API 1.24, so the 2026-10-08 engine upgrade on
    # hpz440 silently dropped every label-declared route while the file
    # route and the certificate kept working. Auto-negotiation landed in
    # v3.6.16 and v3.7.0; the pin must stay on a line that has it.
    assert traefik_image_version() >= (3, 7)
