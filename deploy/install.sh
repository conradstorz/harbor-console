#!/usr/bin/env bash
# Harbor Console installer — sets up the tty1 dashboard service.
# Run as root from a checkout of the repository. Idempotent.
set -euo pipefail

INSTALL_DIR=/opt/harbor-console
UNIT_NAME=harbor-console.service
UNIT_DEST=/etc/systemd/system/${UNIT_NAME}

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
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '*.egg-info' \
  "${REPO_ROOT}/" "${INSTALL_DIR}/"

echo "==> Building virtualenv with uv sync"
( cd "${INSTALL_DIR}" && uv sync )

echo "==> Ensuring 'harbor' service user exists"
if ! id -u harbor >/dev/null 2>&1; then
  useradd --system --user-group --no-create-home --home-dir "${INSTALL_DIR}" --shell /usr/sbin/nologin harbor
fi

usermod -aG docker harbor

echo "==> Setting ownership of ${INSTALL_DIR} to harbor"
chown -R harbor:harbor "${INSTALL_DIR}"

echo "==> Installing systemd unit to ${UNIT_DEST}"
install -m 0644 "${SCRIPT_DIR}/${UNIT_NAME}" "${UNIT_DEST}"
systemctl daemon-reload

echo "==> Masking getty@tty1 (disables the login prompt on tty1 only)"
systemctl mask getty@tty1.service

echo "==> Enabling ${UNIT_NAME} and (re)starting it to load current code"
systemctl enable "${UNIT_NAME}"
systemctl restart "${UNIT_NAME}"

echo
echo "Harbor Console is installed. tty1 now shows the dashboard."
echo "Admin logins remain on tty2-tty6 (Ctrl+Alt+F2 ... F6) and via SSH."
echo
systemctl status "${UNIT_NAME}" --no-pager || true
