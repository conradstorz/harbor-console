#!/usr/bin/env bash
# Harbor Console uninstaller — reverses install.sh.
# Run as root. Pass --purge to also remove /opt/harbor-console.
set -euo pipefail

INSTALL_DIR=/opt/harbor-console
UNIT_DIR=/etc/systemd/system
UNIT_NAMES=(harbor-console.service harbor-console-web.service)

PURGE=0
if [[ "${1:-}" == "--purge" ]]; then
  PURGE=1
fi

if [[ ${EUID} -ne 0 ]]; then
  echo "Error: uninstall.sh must be run as root (try: sudo $0)" >&2
  exit 1
fi

for unit in "${UNIT_NAMES[@]}"; do
  echo "==> Stopping and disabling ${unit}"
  systemctl disable --now "${unit}" 2>/dev/null || true
done

for unit in "${UNIT_NAMES[@]}"; do
  echo "==> Removing ${UNIT_DIR}/${unit}"
  rm -f "${UNIT_DIR}/${unit}"
done
systemctl daemon-reload

echo "==> Restoring the login prompt on tty1"
systemctl unmask getty@tty1.service 2>/dev/null || true
systemctl start getty@tty1.service 2>/dev/null || true

if [[ ${PURGE} -eq 1 ]]; then
  echo "==> Purging ${INSTALL_DIR}"
  rm -rf "${INSTALL_DIR}"
  if id -u harbor >/dev/null 2>&1; then
    echo "==> Removing 'harbor' service user"
    userdel harbor 2>/dev/null || true
  fi
else
  echo "==> Leaving ${INSTALL_DIR} and 'harbor' user in place (use --purge to remove them)"
fi

echo "Harbor Console has been uninstalled."
