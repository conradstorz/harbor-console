#!/usr/bin/env bash
# Harbor Console uninstaller — reverses install.sh.
# Run as root. Pass --purge to also remove /opt/harbor-console.
set -euo pipefail

INSTALL_DIR=/opt/harbor-console
UNIT_NAME=harbor-console.service
UNIT_DEST=/etc/systemd/system/${UNIT_NAME}

PURGE=0
if [[ "${1:-}" == "--purge" ]]; then
  PURGE=1
fi

if [[ ${EUID} -ne 0 ]]; then
  echo "Error: uninstall.sh must be run as root (try: sudo $0)" >&2
  exit 1
fi

echo "==> Stopping and disabling ${UNIT_NAME}"
systemctl disable --now "${UNIT_NAME}" 2>/dev/null || true

echo "==> Removing ${UNIT_DEST}"
rm -f "${UNIT_DEST}"
systemctl daemon-reload

echo "==> Restoring the login prompt on tty1"
systemctl unmask getty@tty1.service 2>/dev/null || true
systemctl start getty@tty1.service 2>/dev/null || true

if [[ ${PURGE} -eq 1 ]]; then
  echo "==> Purging ${INSTALL_DIR}"
  rm -rf "${INSTALL_DIR}"
else
  echo "==> Leaving ${INSTALL_DIR} in place (use --purge to remove it)"
fi

echo "Harbor Console has been uninstalled."
