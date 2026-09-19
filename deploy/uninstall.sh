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

# compose.yaml interpolates TAILNET_ADDRESS and ACME_EMAIL under `${VAR:?}`
# guards, so `docker compose down` fails outright when `.env` is missing --
# which is exactly the half-removed state this script has to survive. Supply
# placeholders (nothing is started, so the values do not matter), and fall
# back to removing the container by name rather than reporting success with
# the edge still running.
if [[ -f "${INSTALL_DIR}/deploy/traefik/compose.yaml" ]]; then
  echo "==> Stopping the edge (Traefik)"
  if ! ( cd "${INSTALL_DIR}/deploy/traefik" && TAILNET_ADDRESS="${TAILNET_ADDRESS:-0.0.0.0}" ACME_EMAIL="${ACME_EMAIL:-none}" docker compose down ); then
    echo "warning: docker compose down failed; removing the traefik container directly" >&2
    docker rm -f traefik >/dev/null 2>&1 || echo "warning: could not remove the traefik container; check 'docker ps'" >&2
  fi
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
echo "/etc/traefik/env (the Cloudflare token) is left in place on purpose; remove it by hand if the host is being retired."
