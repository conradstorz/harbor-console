#!/usr/bin/env bash
# Harbor Console auto-update (ADR 25): deploy main when it has moved.
#
# Run as root by harbor-console-update.service, every five minutes from
# harbor-console-update.timer, from the installed copy under /opt. The
# checkout to follow comes from HARBOR_CHECKOUT, which install.sh writes
# into the unit's drop-in from wherever it was run; nothing here hardcodes
# a path. Quiet when there is nothing to do, loud when it refuses, and it
# never resets, force-pulls or discards work: a diverged or dirty checkout
# is a human's problem and this script says so and stops.
set -euo pipefail

LOCK=/run/harbor-console-update.lock
CHECKOUT="${HARBOR_CHECKOUT:-}"

if [[ ${EUID} -ne 0 ]]; then
  echo "Error: update.sh must run as root (it hands off to install.sh)" >&2
  exit 1
fi
if [[ -z "${CHECKOUT}" ]]; then
  echo "Error: HARBOR_CHECKOUT is not set; re-run install.sh from the checkout to write the unit drop-in" >&2
  exit 1
fi
if [[ ! -d "${CHECKOUT}/.git" ]]; then
  echo "Error: ${CHECKOUT} is not a git checkout" >&2
  exit 1
fi

# One run at a time. install.sh restarts units and brings up compose
# projects; two of it interleaved is worse than one of it late. The lock is
# held on fd 9 through the exec below, so the installer runs under it too.
exec 9>"${LOCK}"
if ! flock -n 9; then
  exit 0
fi

owner=$(stat -c %U "${CHECKOUT}")
git_as_owner() {
  runuser -u "${owner}" -- git -C "${CHECKOUT}" "$@"
}

# What install.sh last finished installing (written by install.sh itself,
# after every install step and before its acceptance check). Compared
# against origin/main instead of the checkout's HEAD, so a pull whose
# install then failed is retried on the next tick rather than counted as
# deployed. Missing means never installed by a version that records it.
DEPLOYED_FILE=/var/lib/harbor-console/deployed-commit
deployed=$(cat "${DEPLOYED_FILE}" 2>/dev/null || true)

branch=$(git_as_owner rev-parse --abbrev-ref HEAD)
if [[ "${branch}" != "main" ]]; then
  echo "Error: ${CHECKOUT} is on '${branch}', not main; not deploying" >&2
  exit 1
fi
if [[ -n "$(git_as_owner status --porcelain --untracked-files=no)" ]]; then
  echo "Error: ${CHECKOUT} has local modifications to tracked files; not deploying" >&2
  exit 1
fi

# Bounded: a stalled connection must not hold the lock forever; the timer cannot start a unit that is still activating.
timeout 120 runuser -u "${owner}" -- git -C "${CHECKOUT}" fetch --quiet origin main
local_head=$(git_as_owner rev-parse HEAD)
remote_head=$(git_as_owner rev-parse origin/main)
if [[ "${local_head}" == "${remote_head}" && "${deployed}" == "${remote_head}" ]]; then
  exit 0
fi
if [[ "${local_head}" != "${remote_head}" ]]; then
  if ! git_as_owner merge-base --is-ancestor "${local_head}" "${remote_head}"; then
    echo "Error: ${CHECKOUT} has commits origin/main does not (HEAD ${local_head:0:9}, origin/main ${remote_head:0:9}); refusing to touch it" >&2
    exit 1
  fi
  echo "updating ${local_head:0:9} -> ${remote_head:0:9}"
  timeout 300 runuser -u "${owner}" -- git -C "${CHECKOUT}" pull --quiet --ff-only origin main
else
  echo "installing ${remote_head:0:9}: the checkout is current but the last install did not record it (${deployed:-none})"
fi

# install.sh is the deploy; its exit code is the acceptance check's.
exec bash "${CHECKOUT}/deploy/install.sh"
