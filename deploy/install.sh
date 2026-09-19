#!/usr/bin/env bash
# Harbor Console installer — sets up the tty1 dashboard and the tailnet status page.
# Run as root from a checkout of the repository. Idempotent.
set -euo pipefail

INSTALL_DIR=/opt/harbor-console
UNIT_DIR=/etc/systemd/system
# Both units are installed together: they share one checkout, one venv and one
# service user, so a partial deploy is a state nobody wants to debug.
UNIT_NAMES=(harbor-console.service harbor-console-web.service)

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

require_root() {
  if [[ ${EUID} -ne 0 ]]; then
    echo "Error: install.sh must be run as root (try: sudo $0)" >&2
    exit 1
  fi
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: '$1' is not on PATH. $2" >&2
    exit 1
  fi
}

require_root
require_cmd uv "Install it first: https://docs.astral.sh/uv/"
require_cmd rsync "Install it with your package manager (e.g. apt install rsync)."

if ! getent group docker >/dev/null 2>&1; then
  echo "Error: the 'docker' group does not exist. The service unit sets SupplementaryGroups=docker and will not start without it. Install Docker first." >&2
  exit 1
fi

echo "==> Checking edge prerequisites"
require_cmd docker "Install Docker first."
require_cmd tailscale "Install Tailscale first."
require_cmd openssl "Needed to generate the Traefik dashboard/API password (ADR 16)."
# The ufw rule below names the bridge interface, so the harbor network has to
# carry a predictable one. A network created without it (by an older installer,
# or by hand) would get a generated br-<id> name the rule cannot match, and the
# edge would silently fail to reach the page. Check before anything is changed.
if docker network inspect harbor >/dev/null 2>&1; then
  harbor_bridge=$(docker network inspect harbor -f '{{index .Options "com.docker.network.bridge.name"}}' 2>/dev/null || true)
  if [[ "${harbor_bridge}" != "br-harbor" ]]; then
    echo "Error: the 'harbor' Docker network exists without the fixed bridge name 'br-harbor' (found: '${harbor_bridge:-none}')." >&2
    echo "  The ufw rule that lets the edge reach the status page names that interface, so the network has to be recreated:" >&2
    echo "    stop everything attached to it (e.g. cd /opt/harbor-console/deploy/traefik && docker compose down)," >&2
    echo "    then: docker network rm harbor" >&2
    echo "  and re-run this installer." >&2
    exit 1
  fi
fi
if [[ ! -f /etc/traefik/env ]]; then
  echo "Error: /etc/traefik/env is missing. Create it (mode 0600) with:" >&2
  echo "  CF_DNS_API_TOKEN=<cloudflare token with Zone.DNS edit on ohr3023.org>" >&2
  echo "  ACME_EMAIL=<address for Let's Encrypt notices>" >&2
  echo "  (plain KEY=value lines: no export, no quotes -- the file is read by both bash and compose)" >&2
  exit 1
fi
TAILNET_ADDRESS=$(tailscale ip -4 2>/dev/null | head -n1 || true)
if [[ -z "${TAILNET_ADDRESS}" ]]; then
  echo "Error: tailscale ip -4 returned nothing; the edge binds the tailnet address only." >&2
  exit 1
fi
# Parsed, never sourced: `/etc/traefik/env` is root-owned but not root-
# authored -- it is edited by hand on the host -- and `. /etc/traefik/env`
# would execute its contents as this script's own shell, as root. A file
# that is supposed to hold two KEY=value lines has no business being able
# to run a command; grep only ever reads it as text.
# `|| true`: under pipefail, a grep that matches nothing makes this whole
# pipeline exit non-zero even though the empty result it passes along is
# exactly correct -- an ACME_EMAIL genuinely missing from the file would
# otherwise kill this script silently here, under set -e, before the
# intended "is it empty" check below ever ran to explain why.
ACME_EMAIL=$(grep -E '^ACME_EMAIL=' /etc/traefik/env 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '\r' || true)
if [[ -z "${ACME_EMAIL}" ]]; then
  echo "Error: ACME_EMAIL is not set in /etc/traefik/env." >&2
  exit 1
fi

echo "==> Syncing repository to ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
rsync -a --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '.uv-python' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '*.egg-info' \
  --exclude 'deploy/traefik/.env' \
  --exclude 'deploy/traefik/dynamic/harbor.yml' \
  "${REPO_ROOT}/" "${INSTALL_DIR}/"

# The service runs as the unprivileged 'harbor' user with ProtectHome=yes. If the host
# has no Python 3.13+, uv installs a managed CPython; by default it lands under root's
# home (/root/.local/share/uv/python), which harbor cannot read and ProtectHome hides —
# the venv symlinks to it and the service dies with 203/EXEC. Pin the managed interpreter
# inside INSTALL_DIR so the chown below makes it harbor-readable and ProtectHome-safe.
export UV_PYTHON_INSTALL_DIR="${INSTALL_DIR}/.uv-python"

# Self-heal an install left by an older installer (or a moved interpreter): if an
# existing venv's Python resolves into a home directory, harbor can't exec it under
# ProtectHome, and `uv sync` would happily keep that venv. Drop it so uv rebuilds
# against the pinned location above. Venvs on system paths (/usr) or already inside
# INSTALL_DIR are fine and left untouched.
if [[ -e "${INSTALL_DIR}/.venv/bin/python" ]]; then
  current_py=$(readlink -f "${INSTALL_DIR}/.venv/bin/python" 2>/dev/null || true)
  case "${current_py}" in
    /root/*|/home/*)
      echo "==> Removing stale venv (interpreter under a home dir: ${current_py})"
      rm -rf "${INSTALL_DIR}/.venv"
      ;;
  esac
fi

echo "==> Building virtualenv with uv sync"
( cd "${INSTALL_DIR}" && uv sync )

echo "==> Ensuring 'harbor' service user exists"
if ! id -u harbor >/dev/null 2>&1; then
  useradd --system --user-group --no-create-home --home-dir "${INSTALL_DIR}" --shell /usr/sbin/nologin harbor
fi

usermod -aG docker harbor

echo "==> Setting ownership of ${INSTALL_DIR} to harbor"
chown -R harbor:harbor "${INSTALL_DIR}"

for unit in "${UNIT_NAMES[@]}"; do
  echo "==> Installing systemd unit to ${UNIT_DIR}/${unit}"
  install -m 0644 "${SCRIPT_DIR}/${unit}" "${UNIT_DIR}/${unit}"
done
systemctl daemon-reload

echo "==> Masking getty@tty1 (disables the login prompt on tty1 only)"
systemctl mask getty@tty1.service

# enable, then restart -- not `enable --now`, which starts a stopped unit but
# leaves a running one on the old code. This script is the update path.
for unit in "${UNIT_NAMES[@]}"; do
  echo "==> Enabling ${unit} and (re)starting it to load current code"
  systemctl enable "${unit}"
  systemctl restart "${unit}"
done

# The edge starts only after the units have moved: until harbor-console-web is
# restarted on the new code it still holds the tailnet address on :80, which is
# the port Traefik is about to publish.
echo "==> Preparing the edge (Traefik)"
chmod 0600 /etc/traefik/env
# The bridge name is fixed so the ufw rule below can name the interface; the
# prerequisite check above refuses an existing network that lacks it.
docker network inspect harbor >/dev/null 2>&1 || docker network create -o com.docker.network.bridge.name=br-harbor harbor
# ADR 16: Portainer's read-write Docker socket lives on its own network, not
# on `harbor` where every routed project container now sits. No fixed
# bridge name needed here -- nothing scopes a host-facing rule to it, only
# Traefik and Portainer ever join it.
docker network inspect admin >/dev/null 2>&1 || docker network create admin

# ADR 16: Traefik's own /api is gated by HTTP Basic Auth rather than served
# with none at all (`--api.insecure=true` would answer it, unauthenticated,
# to any container on `harbor`, not only to the loopback-only host publish).
# The password is generated once, on first install, and kept in
# /etc/traefik/env alongside the other secrets; every run derives the
# htpasswd file Traefik reads and the plaintext copy harbor-console-web
# reads from that one source, so a hand-rotated password takes effect on
# the next install.sh run with no other step.
if ! grep -qE '^TRAEFIK_DASHBOARD_PASSWORD=' /etc/traefik/env 2>/dev/null; then
  new_password=$(openssl rand -base64 24)
  printf 'TRAEFIK_DASHBOARD_PASSWORD=%s\n' "${new_password}" >> /etc/traefik/env
  chmod 0600 /etc/traefik/env
  echo "==> Generated a new Traefik dashboard/API password -- it will not be shown again:"
  echo "    ${new_password}"
fi
# `|| true`: same reason as the ACME_EMAIL extraction above.
TRAEFIK_DASHBOARD_PASSWORD=$(grep -E '^TRAEFIK_DASHBOARD_PASSWORD=' /etc/traefik/env | tail -n1 | cut -d= -f2- | tr -d '\r' || true)
if [[ -z "${TRAEFIK_DASHBOARD_PASSWORD}" ]]; then
  echo "Error: TRAEFIK_DASHBOARD_PASSWORD in /etc/traefik/env is empty." >&2
  exit 1
fi
# Traefik itself reads the htpasswd form (root-only, mounted read-only into
# the container). harbor-console-web -- running as the unprivileged
# `harbor` user, not root -- reads the plaintext form instead, so it is
# owned root:harbor and group-readable rather than root-only like
# /etc/traefik/env, which also carries the Cloudflare token and stays
# root-only.
printf 'harbor:%s\n' "$(openssl passwd -apr1 "${TRAEFIK_DASHBOARD_PASSWORD}")" > /etc/traefik/dashboard-users
chmod 0600 /etc/traefik/dashboard-users
printf '%s\n' "${TRAEFIK_DASHBOARD_PASSWORD}" > /etc/traefik/dashboard-password
chown "root:harbor" /etc/traefik/dashboard-password
chmod 0640 /etc/traefik/dashboard-password

TRAEFIK_DIR="${INSTALL_DIR}/deploy/traefik"
printf 'TAILNET_ADDRESS=%s\nACME_EMAIL=%s\n' "${TAILNET_ADDRESS}" "${ACME_EMAIL}" > "${TRAEFIK_DIR}/.env"
sed "s/@TAILNET_ADDRESS@/${TAILNET_ADDRESS}/" "${TRAEFIK_DIR}/dynamic/harbor.yml.in" > "${TRAEFIK_DIR}/dynamic/.harbor.yml.tmp"
mv -f "${TRAEFIK_DIR}/dynamic/.harbor.yml.tmp" "${TRAEFIK_DIR}/dynamic/harbor.yml"
( cd "${TRAEFIK_DIR}" && docker compose up -d --remove-orphans )

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  # Scoped to Traefik's own address on `harbor`, not the whole bridge
  # interface: `ufw allow in on br-harbor` would admit every container on
  # that network to the status page's port, not only Traefik (ADR 16).
  # Traefik's own connection to the page carries its actual container
  # address as source -- ordinary bridge routing, not the port-publish NAT
  # that makes host-loopback traffic arrive as the gateway address -- so
  # this is the one address the rule needs to name. Applied after
  # `docker compose up` above, once that address is known.
  ufw_tag='harbor-console: traefik -> page'
  # Idempotent: drop any earlier instance of this rule first, in case
  # Traefik's address on `harbor` has changed since the last install (most
  # likely a network recreation), so a stale allow for an address nothing
  # uses any more is never left standing alongside a fresh one.
  while true; do
    # `|| true`: under pipefail, this pipeline exits non-zero whenever no
    # rule matches -- the ordinary, expected "nothing left to delete" case
    # -- because the two greps in the middle report no-match as failure
    # even though the empty output they pass along is exactly correct. That
    # silently killed this script, under set -e, on the very first
    # iteration: with the loop never reaching the `[[ -z ]]` check that was
    # supposed to break it out cleanly. Confirmed by reproducing it locally
    # before this fix went anywhere near hpz440 again.
    rule_num=$(ufw status numbered 2>/dev/null | grep -F "${ufw_tag}" | head -n1 | grep -oE '^\[ *[0-9]+\]' | tr -dc '0-9' || true)
    if [[ -z "${rule_num}" ]]; then
      break
    fi
    # `--force` skips ufw's interactive "Proceed with operation (y|n)?"
    # prompt. `yes | ufw delete` looked equivalent and is not: under
    # `pipefail`, `yes` is killed by SIGPIPE the instant `ufw` stops
    # reading, and that non-zero exit status propagates as the pipeline's
    # own -- even though `ufw delete` itself succeeded -- which trips `set
    # -e` and kills this script silently, right here, with no error text.
    # That happened live: it deleted the pre-ADR-16 rule this loop was
    # meant to replace and then exited before the replacement was added,
    # leaving no rule in place at all until the next install.sh run.
    ufw --force delete "${rule_num}" >/dev/null
  done
  traefik_harbor_ip=$(docker inspect traefik -f '{{(index .NetworkSettings.Networks "harbor").IPAddress}}' 2>/dev/null || true)
  if [[ -n "${traefik_harbor_ip}" ]]; then
    ufw allow in on br-harbor from "${traefik_harbor_ip}" to "${TAILNET_ADDRESS}" port 8100 proto tcp comment "${ufw_tag}" >/dev/null
  else
    echo "warning: could not determine Traefik's address on the harbor network; the page will not be reachable through the edge until this is fixed and install.sh is re-run." >&2
  fi
fi

echo "==> Bringing up hosted infrastructure (Portainer, Watchtower)"
( cd "${INSTALL_DIR}/deploy/hosted" && docker compose -p hosted up -d --remove-orphans )

echo
echo "Harbor Console is installed. tty1 now shows the dashboard."
echo "The status page is https://harbor.hpz440.ohr3023.org/ (direct: http://${TAILNET_ADDRESS}:8100/)."
echo "Admin logins remain on tty2-tty6 (Ctrl+Alt+F2 ... F6) and via SSH."
echo
for unit in "${UNIT_NAMES[@]}"; do
  systemctl status "${unit}" --no-pager || true
done
