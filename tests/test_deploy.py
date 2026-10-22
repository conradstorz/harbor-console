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


def test_the_web_unit_owns_a_state_directory_for_the_gpu_history():
    text = (REPO / "deploy" / "harbor-console-web.service").read_text(encoding="utf-8")

    assert "StateDirectory=harbor-console" in text


def test_uninstall_removes_the_state_directory():
    text = (REPO / "deploy" / "uninstall.sh").read_text(encoding="utf-8")

    assert "/var/lib/harbor-console" in text


HOSTED_COMPOSE = REPO / "deploy" / "hosted" / "compose.yaml"


def watchtower_image() -> tuple[str, tuple[int, int, int]]:
    text = HOSTED_COMPOSE.read_text(encoding="utf-8")
    match = re.search(
        r"^\s*image:\s*([\w./-]+/watchtower):(\d+)\.(\d+)\.(\d+)\s*$", text, re.MULTILINE
    )
    assert match, "deploy/hosted/compose.yaml must pin `image: <repo>/watchtower:<x.y.z>`"
    return match.group(1), (int(match.group(2)), int(match.group(3)), int(match.group(4)))


def test_watchtower_is_the_maintained_fork_and_negotiates_the_docker_api_version():
    # containrrr/watchtower was archived on 2025-12-17; its last image (v1.7.1,
    # 2023-11-11) pins Docker API 1.25, which Engine 29 refuses, so after the
    # 2026-10-08 upgrade on hpz440 it crash-looped 637 times with no row or
    # finding to show for it. nicholas-fedor/watchtower is the maintained fork,
    # same flags, same env vars, same opt-in label, and autonegotiates the API
    # version; 1.23.1 was verified against API 1.56 on 2026-10-21. Pinned to a
    # release so an update is a deliberate commit, like the Traefik pin.
    repo, version = watchtower_image()
    assert repo == "nickfedor/watchtower"
    assert version >= (1, 23, 1)


def compose_services(path: Path) -> list[str]:
    """Top-level service names: two-space-indented keys under `services:`."""
    text = path.read_text(encoding="utf-8")
    body = text.split("services:", 1)[1]
    body = re.split(r"^\S", body, maxsplit=1, flags=re.MULTILINE)[0]
    return re.findall(r"^  ([A-Za-z][\w-]*):\s*$", body, re.MULTILINE)


def test_every_container_this_repo_deploys_declares_itself_platform():
    # ADR 24: the platform-containers check judges only containers that
    # carry harbor.platform=true. A service added here without it is a
    # container the check silently ignores.
    for path, expected in ((TRAEFIK_COMPOSE, ["traefik"]), (HOSTED_COMPOSE, ["portainer", "watchtower"])):
        text = path.read_text(encoding="utf-8")
        assert compose_services(path) == expected, path
        assert text.count('harbor.platform: "true"') == len(expected), path


UPDATE_SCRIPT = REPO / "deploy" / "update.sh"
UPDATE_SERVICE = REPO / "deploy" / "harbor-console-update.service"
UPDATE_TIMER = REPO / "deploy" / "harbor-console-update.timer"


def test_the_updater_only_ever_fast_forwards_as_the_checkouts_owner():
    # ADR 25. The one write to the checkout is a fast-forward pull, run as
    # whoever owns the checkout so root never touches its objects. Anything
    # that could discard local work is a plan failure, not a flag.
    text = UPDATE_SCRIPT.read_text(encoding="utf-8")
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    # Command invocations only: not prose in an echo, not the `.git` directory test.
    git_lines = [
        line
        for line in code
        if re.search(r"(^|[\s(])(git|git_as_owner)(\s|$)", line) and not line.lstrip().startswith("echo")
    ]

    for line in git_lines:
        assert "runuser -u" in line or "git_as_owner" in line, line
        for forbidden in (" reset", " clean", " rebase", " checkout", " switch", " -f ", " --force", " --hard"):
            assert forbidden not in line, line
    assert any("pull --quiet --ff-only" in line for line in git_lines)
    assert any("merge-base --is-ancestor" in line for line in git_lines)
    assert any("--untracked-files=no" in line for line in git_lines)
    assert "flock -n" in text
    assert "LOCK=/run/harbor-console-update.lock" in text
    assert "\r" not in text, "update.sh must be LF; CRLF shell does not run"


def test_the_updater_bounds_its_network_calls_and_retries_a_failed_install():
    text = UPDATE_SCRIPT.read_text(encoding="utf-8")

    assert "timeout 120 runuser" in text and "fetch --quiet origin main" in text
    assert "timeout 300 runuser" in text and "pull --quiet --ff-only origin main" in text
    assert "DEPLOYED_FILE=/var/lib/harbor-console-update/deployed-commit" in text
    assert '"${deployed}" == "${remote_head}"' in text


def test_the_updater_hands_off_to_the_installer_from_the_checkout():
    text = UPDATE_SCRIPT.read_text(encoding="utf-8")

    assert 'bash "${CHECKOUT}/deploy/install.sh"' in text
    assert "HARBOR_CHECKOUT" in text
    assert "export HARBOR_UPDATE_LOCKED=1" in text


def test_the_update_timer_fires_every_five_minutes_and_only_the_timer_is_installed():
    timer = UPDATE_TIMER.read_text(encoding="utf-8")
    service = UPDATE_SERVICE.read_text(encoding="utf-8")

    assert "OnBootSec=5min\n" in timer
    assert "OnUnitActiveSec=5min\n" in timer
    assert "Persistent=false\n" in timer
    assert "WantedBy=timers.target\n" in timer
    assert "Type=oneshot\n" in service
    assert "ExecStart=/bin/bash /opt/harbor-console/deploy/update.sh\n" in service
    assert "[Install]" not in service, "the timer starts the service; it is never enabled itself"


UNINSTALL = REPO / "deploy" / "uninstall.sh"


def test_the_installer_installs_the_updater_and_enables_only_its_timer():
    text = INSTALL.read_text(encoding="utf-8")

    assert "harbor-console-update.service" in text
    assert "systemctl enable --now harbor-console-update.timer" in text
    assert "systemctl enable harbor-console-update.service" not in text
    assert "harbor-console-update.service.d/checkout.conf" in text
    assert 'Environment="HARBOR_CHECKOUT=${REPO_ROOT}"' in text
    assert 'Environment="PATH=${uv_dir}:' in text
    assert 'runuser -u "${checkout_owner}" -- git -C "${REPO_ROOT}" rev-parse HEAD' in text
    assert "DEPLOYED_FILE=/var/lib/harbor-console-update/deployed-commit" in text
    assert text.index("deployed-commit") < text.index('harbor-console-check" 2>&1')
    assert text.index("deployed-commit") < text.index("systemctl enable --now harbor-console-update.timer")
    assert 'UPDATE_LOCK=/run/harbor-console-update.lock' in text
    assert 'chown -R root:root "${INSTALL_DIR}/deploy"' in text
    assert text.index('chown -R harbor:harbor "${INSTALL_DIR}"') < text.index('chown -R root:root "${INSTALL_DIR}/deploy"')
    assert 'if [[ -z "${HARBOR_UPDATE_LOCKED:-}" ]]; then' in text


def test_the_installer_writes_the_dropin_before_the_daemon_reload_that_enables_the_timer():
    text = INSTALL.read_text(encoding="utf-8")

    assert text.index("checkout.conf") < text.index("systemctl enable --now harbor-console-update.timer")


def test_uninstall_removes_the_updater():
    text = UNINSTALL.read_text(encoding="utf-8")

    assert "harbor-console-update.timer" in text
    assert "harbor-console-update.service" in text
    assert "harbor-console-update.service.d" in text
    assert "/var/lib/harbor-console-update" in text
    assert "flock 9" in text
