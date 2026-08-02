# Harbor Console Deployment — Dedicated User Increment

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** Run the service as a dedicated unprivileged `harbor` system user (in the `docker` group so the container count still works) with a conservative slice of systemd sandboxing, instead of `root`. Amends ADR 0004.

**Architecture:** The systemd unit gains `User=harbor` / `Group=harbor` / `SupplementaryGroups=docker` plus `NoNewPrivileges`/`ProtectHome`/`PrivateTmp`. `install.sh` idempotently creates the `harbor` system user, adds it to `docker`, and chowns `/opt/harbor-console`. `uninstall.sh --purge` removes the user. tty1 still renders because systemd (root) opens `/dev/tty1` and hands the fd to the process.

## Global Constraints

- Service user: `harbor`, a **system** user (`useradd --system --no-create-home --home-dir /opt/harbor-console --shell /usr/sbin/nologin`), no login.
- `harbor` must be added to the `docker` group (Docker count matters on the target); if the `docker` group is absent, warn and continue (count degrades to 0).
- Conservative hardening ONLY: `NoNewPrivileges=yes`, `ProtectHome=yes`, `PrivateTmp=yes`. **Never** add `PrivateDevices` (severs `/dev/tty1`) or `PrivateNetwork` (breaks the IPv4 probe).
- Keep everything else from the shipped unit (tty1 binding, `Restart=always`, `RestartSec=2`, `StartLimitIntervalSec=0`, `Environment=TERM=linux`).
- All scripts remain `#!/usr/bin/env bash` + `set -euo pipefail`, idempotent, fail-fast.
- Verification reality unchanged: only `bash -n` + grep run on the Windows dev host; `shellcheck`, `systemd-analyze verify`, `systemd-analyze security`, and the boot smoke test are Linux/target gates — do not fake them.

---

### Task N1: unit runs as harbor with conservative hardening

**Files:** Modify `deploy/harbor-console.service`

- [ ] **Step 1: Edit the `[Service]` section**

Replace the single line `User=root` with the following block (keep it where `User=root` was, i.e. right after `RestartSec=2`):

```ini
User=harbor
Group=harbor
SupplementaryGroups=docker
NoNewPrivileges=yes
ProtectHome=yes
PrivateTmp=yes
```

Leave every other line of the unit unchanged. The full `[Service]` section becomes:

```ini
[Service]
Type=simple
ExecStart=/opt/harbor-console/.venv/bin/harbor-console
Restart=always
RestartSec=2
User=harbor
Group=harbor
SupplementaryGroups=docker
NoNewPrivileges=yes
ProtectHome=yes
PrivateTmp=yes
TTYPath=/dev/tty1
StandardInput=tty-force
StandardOutput=tty
StandardError=journal
TTYReset=yes
TTYVHangup=yes
Environment=TERM=linux
```

- [ ] **Step 2: Sanity-check (dev host)**

Run each on its own line (no `&&`):
```bash
grep -c '^User=harbor$' deploy/harbor-console.service        # expect 1
grep -c '^User=root$' deploy/harbor-console.service          # expect 0
grep -c '^SupplementaryGroups=docker$' deploy/harbor-console.service  # expect 1
grep -c '^PrivateDevices' deploy/harbor-console.service      # expect 0 (must NOT be present)
```

- [ ] **Step 3: Commit**

```bash
git add deploy/harbor-console.service
git commit -m "feat(deploy): run service as harbor user with conservative hardening"
```

---

### Task N2: install/uninstall manage the harbor user

**Files:** Modify `deploy/install.sh`, `deploy/uninstall.sh`

- [ ] **Step 1: install.sh — insert user management**

In `deploy/install.sh`, immediately AFTER the `( cd "${INSTALL_DIR}" && uv sync )` line and its blank line, and BEFORE the `echo "==> Installing systemd unit ..."` block, insert:

```bash
echo "==> Ensuring 'harbor' service user exists"
if ! id -u harbor >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir "${INSTALL_DIR}" --shell /usr/sbin/nologin harbor
fi

if getent group docker >/dev/null 2>&1; then
  usermod -aG docker harbor
else
  echo "Note: 'docker' group not found — Docker container count will show 0." >&2
fi

echo "==> Setting ownership of ${INSTALL_DIR} to harbor"
chown -R harbor:harbor "${INSTALL_DIR}"
```

- [ ] **Step 2: uninstall.sh — remove the user under --purge**

In `deploy/uninstall.sh`, replace the existing purge block:

```bash
if [[ ${PURGE} -eq 1 ]]; then
  echo "==> Purging ${INSTALL_DIR}"
  rm -rf "${INSTALL_DIR}"
else
  echo "==> Leaving ${INSTALL_DIR} in place (use --purge to remove it)"
fi
```

with:

```bash
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
```

- [ ] **Step 3: Verify syntax (dev host)**

Run each on its own line:
```bash
bash -n deploy/install.sh
bash -n deploy/uninstall.sh
```
Both: exit 0, no output.

- [ ] **Step 4: Commit**

```bash
git add deploy/install.sh deploy/uninstall.sh
git commit -m "feat(deploy): create harbor user on install, remove on purge"
```

---

### Task N3: record ADR 0005 and update docs

**Files:** Create `docs/adr/0005-run-as-harbor-user.md`; Modify `docs/adr/README.md`, `docs/adr/0004-systemd-tty1-service.md`, `docs/deployment.md`, `docs/superpowers/plans/2026-08-01-deployment.md`

- [ ] **Step 1: Create `docs/adr/0005-run-as-harbor-user.md`** (first line is the heading, no outer fence):

```markdown
# 5. Run as a dedicated `harbor` user, not root

Date: 2026-08-01

## Status

Accepted — amends the run-as-user decision of [ADR 4](0004-systemd-tty1-service.md).

## Context

ADR 4 chose to run the service as `root` for MVP simplicity. Preparing to deploy
on the real target (an HP Z440 server) reopened the question. The dashboard is a
read-only metrics renderer with no network listener and no untrusted input, so
its attack surface is small — but running it under a dedicated account gives
cleaner ownership of `/opt/harbor-console`, attributable logs, and a
least-privilege posture that is cheap to establish now and awkward to retrofit.

The one elevated dependency is the Docker container count, which reads the Docker
socket and therefore needs `docker` group membership. That group is effectively
root-equivalent, so it caps how much isolation a non-root user actually buys —
the change is mostly operational hygiene plus modest defense-in-depth.

## Decision

Run the service as a dedicated **system** user `harbor`
(`useradd --system --no-create-home --shell /usr/sbin/nologin`), added to the
`docker` group so the container count keeps working. The unit sets
`User=harbor` / `Group=harbor` / `SupplementaryGroups=docker` and a conservative
slice of sandboxing: `NoNewPrivileges=yes`, `ProtectHome=yes`, `PrivateTmp=yes`.
`install.sh` creates the user and `chown`s `/opt/harbor-console`;
`uninstall.sh --purge` removes it.

## Consequences

- tty1 still renders: systemd (PID 1, as root) opens `/dev/tty1` and passes the
  descriptor to the process, so an unprivileged `harbor` writes through the
  inherited fd. Verify this in the on-target smoke test.
- `docker`-group membership is ~root-equivalent, so the security gain over ADR 4
  is limited; the win is primarily hygiene and defense-in-depth.
- `SupplementaryGroups=docker` requires the `docker` group to exist on the host,
  or the unit fails to start; the target runs Docker, so this holds. `install.sh`
  warns if the group is absent.
- Heavier sandboxing (`ProtectSystem=strict`, `RestrictAddressFamilies`, …) is
  intentionally deferred and should be tuned on the box with
  `systemd-analyze security harbor-console.service` — it can subtly break the
  venv exec, the Docker socket, or the IPv4 probe. `PrivateDevices` and
  `PrivateNetwork` must never be added (they sever tty1 and the IPv4 probe).
```

- [ ] **Step 2: Add the index row** — in `docs/adr/README.md`, add after the `0004` row:

```markdown
| 0005 | [Run as a dedicated `harbor` user, not root](0005-run-as-harbor-user.md) | Accepted |
```

- [ ] **Step 3: Point ADR 0004 at the amendment** — in `docs/adr/0004-systemd-tty1-service.md`, change its `## Status` body from `Accepted` to:

```markdown
Accepted — the run-as-root aspect is amended by [ADR 5](0005-run-as-harbor-user.md).
```

- [ ] **Step 4: Update `docs/deployment.md`**

In the "### What it changes" list, add this bullet after the `/opt/harbor-console` copy bullet:

```markdown
- Creates a `harbor` system user (added to the `docker` group) that owns
  `/opt/harbor-console` and runs the service.
```

In the Uninstall section, change the `--purge` comment so it reads:

```markdown
sudo deploy/uninstall.sh --purge  # also removes /opt/harbor-console and the harbor user
```

In the "## Smoke test" list, insert a new step after the reboot step (renumber the rest):

```markdown
3. Confirm the Docker container count is correct (not stuck at 0) — verifies the
   `harbor` user's `docker`-group access.
```

- [ ] **Step 5: Amend the original plan** — at the very top of `docs/superpowers/plans/2026-08-01-deployment.md`, immediately after the first-line H1 heading, insert:

```markdown

> **Amended 2026-08-01:** the service runs as a dedicated `harbor` user with
> conservative systemd hardening rather than `root` — see
> [ADR 5](../../adr/0005-run-as-harbor-user.md) and
> [the dedicated-user increment plan](2026-08-01-deployment-nonroot.md). The
> `User=root` and install/uninstall snippets below reflect the original design.
```

- [ ] **Step 6: Verify (dev host)** — run each on its own line:
```bash
test -f docs/adr/0005-run-as-harbor-user.md
grep -c "0005-run-as-harbor-user.md" docs/adr/README.md          # expect >=1
grep -c "harbor" docs/deployment.md                              # expect >=1
head -1 docs/adr/0005-run-as-harbor-user.md                      # '# 5. Run as a dedicated `harbor` user, not root'
```

- [ ] **Step 7: Commit**

```bash
git add docs/adr/0005-run-as-harbor-user.md docs/adr/README.md docs/adr/0004-systemd-tty1-service.md docs/deployment.md docs/superpowers/plans/2026-08-01-deployment.md
git commit -m "docs(adr): record ADR 0005 (run as harbor user) and update guides"
```

---

## Final verification (Linux target)

- [ ] `shellcheck deploy/install.sh deploy/uninstall.sh` → no findings.
- [ ] `systemd-analyze verify deploy/harbor-console.service` → exit 0.
- [ ] `systemd-analyze security harbor-console.service` → review exposure score; tune heavier hardening as a follow-up.
- [ ] Smoke test incl. new step: dashboard renders on tty1 as `harbor`, Docker count is correct, tty2–tty6 still log in, uninstall `--purge` removes the user.
