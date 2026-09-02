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

echo "==> Syncing repository to ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
rsync -a --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '.uv-python' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '*.egg-info' \
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

echo
echo "Harbor Console is installed. tty1 now shows the dashboard."
echo "The status page is served to the tailnet by harbor-console-web, on the"
echo "port services.toml leases it."
echo "Admin logins remain on tty2-tty6 (Ctrl+Alt+F2 ... F6) and via SSH."
echo
for unit in "${UNIT_NAMES[@]}"; do
  systemctl status "${unit}" --no-pager || true
done
