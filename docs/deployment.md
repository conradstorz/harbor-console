# Deploying Harbor Console

Harbor Console deploys as **two** systemd units from one checkout, one
virtualenv and one service user:

| Unit | What it does | Where you see it |
| --- | --- | --- |
| `harbor-console.service` | Takes over the physical console (`tty1`) at boot and shows the dashboard. | The attached monitor |
| `harbor-console-web.service` | Serves the read-only status page to the tailnet, on the port `services.toml` leases it. | `http://<host>:<leased port>/` from any tailnet peer |

They are installed, enabled and restarted together — a partial deploy is a
state nobody wants to debug — but they have **independent lifetimes**: neither
can take the other down. This guide covers install, verification, diagnosis,
update and removal of both.

## Prerequisites

- A systemd-based Linux host.
- Root access (`sudo`).
- [`uv`](https://docs.astral.sh/uv/) and `rsync` installed on the host.
  `install.sh` fails fast without either.
- Docker installed — the service user joins the `docker` group, and both units
  set `SupplementaryGroups=docker`. `install.sh` fails fast if the `docker`
  group does not exist.
- **Tailscale installed, running, and holding an IPv4 address.** Check with:

  ```bash
  tailscale ip -4     # must print one IPv4 address, e.g. 100.69.239.123
  ```

  `harbor-console-web` binds that address and only that address. There is no
  fallback to `0.0.0.0`, no `--host`, and no dev mode — binding *is* the access
  control ([ADR 7](adr/0007-bind-tailscale-address-only.md)).

  > **`install.sh` does not check for this.** It fails fast on a missing
  > `docker` group but not on a missing `tailscale` binary. On a host without
  > Tailscale the installer reports success and `harbor-console-web` then
  > crash-loops every two seconds *forever*: the unit sets `Restart=always`
  > with `StartLimitIntervalSec=0`, so systemd never gives up and never marks
  > the unit failed. See [A unit that will not stay up](#a-unit-that-will-not-stay-up).

- **`harbor-console` must be declared in `services.toml`** — exactly once, as
  project `harbor-console`, port name `web`. It is (port 8090 on `hpz440`). The
  service takes its port from its own lease and refuses to start without one;
  every service is declared, including this one.

## Install

Copy this repository to the server, then run:

```bash
sudo deploy/install.sh
```

The installer is idempotent — re-run it any time to update.

### What it changes

- Copies the repo to `/opt/harbor-console` and builds `.venv` with `uv sync`.
  If the host has no Python 3.13+, `uv` fetches a managed interpreter into
  `/opt/harbor-console/.uv-python` (not root's home) so the unprivileged `harbor`
  user — running under `ProtectHome=yes` — can execute it.
- Creates a `harbor` system user (added to the `docker` group) that owns
  `/opt/harbor-console` and runs both services.
- Installs `/etc/systemd/system/harbor-console.service` **and**
  `/etc/systemd/system/harbor-console-web.service`.
- Masks `getty@tty1.service` (removes the login prompt on **tty1 only**).
- Enables and restarts **both** units, then prints `systemctl status` for each.

The ledger the web service reads is `/opt/harbor-console/services.toml` — the
copy `rsync` just made, not the one in your checkout. Granting a lease on the
dev box therefore does not reach the server until you re-run the installer.

## Admin access (important)

Masking `getty@tty1` removes the interactive login on `tty1` only. Virtual
terminals **tty2–tty6 keep their normal logins** — reach them with
Ctrl+Alt+F2 … F6 — and SSH is unaffected. You cannot be locked out at the
physical keyboard, even if the dashboard crash-loops.

## Update

Re-run the installer from an **updated checkout of the repository**:

```bash
sudo deploy/install.sh
```

It re-syncs `/opt/harbor-console`, rebuilds the virtualenv with `uv sync`, and
restarts both services so the new code takes effect. Note:
`/opt/harbor-console` is a copy, not a git clone — pull updates in your
checkout, then re-run the installer.

## Uninstall

```bash
sudo deploy/uninstall.sh          # stops and removes both units, restores the tty1 login prompt
sudo deploy/uninstall.sh --purge  # also removes /opt/harbor-console and the harbor user
```

Both units are disabled, stopped and removed, and `getty@tty1` is unmasked and
started. Without `--purge`, `/opt/harbor-console` and the `harbor` user are
left in place.

## Checking status and logs

Every command takes a unit name, so run the pair:

```bash
systemctl status harbor-console          # the tty1 dashboard
systemctl status harbor-console-web      # the tailnet status page

journalctl -u harbor-console -b          # this boot's logs, dashboard
journalctl -u harbor-console-web -b      # this boot's logs, status page
journalctl -u harbor-console-web -f      # follow, e.g. while diagnosing a crash loop
```

A healthy `harbor-console-web` logs exactly one line per start:

```
harbor-console-web listening on http://100.69.239.123:8090/
```

Request logging is deliberately off — journald already timestamps what matters.
So after that line, silence is the normal state.

Confirm what it actually bound:

```bash
sudo ss -ltnp | grep 8090
```

The local address must be the Tailscale address. **If it ever shows `0.0.0.0`,
something is very wrong** — the page is an inventory of every service on the
host, and `0.0.0.0` publishes it to the whole LAN.

## Troubleshooting

### The dashboard (`harbor-console`)

- Nothing on the monitor: confirm the unit is active and `getty@tty1` is
  masked (`systemctl is-enabled getty@tty1`); switch to the console with
  Ctrl+Alt+F1.
- `status=203/EXEC` / `Permission denied` executing `.venv/bin/harbor-console`:
  the venv's Python points somewhere the `harbor` user can't reach (e.g. a
  `uv`-managed interpreter under `/root`, which `ProtectHome=yes` also hides). Re-run
  the installer — it pins the interpreter under `/opt/harbor-console/.uv-python`.
  Verify with `readlink -f /opt/harbor-console/.venv/bin/python` (must resolve
  inside `/opt/harbor-console`). This affects **both** units, since they share
  the venv.

### A unit that will not stay up

`harbor-console-web` has **four startup refusals**, plus a bind that can fail.
Each happens before anything is bound, prints one `error:` line to the journal,
and exits 1. `Restart=always` with `RestartSec=2` and
`StartLimitIntervalSec=0` means systemd retries every two seconds indefinitely
and never marks the unit failed — so the symptom of every refusal is the same:
`systemctl status harbor-console-web` shows `activating (auto-restart)` and a
climbing restart count, and the reason is only in the journal.

Read the reason first:

```bash
journalctl -u harbor-console-web -b | grep '^.*error:'
```

| Journal line | What it means | Fix |
| --- | --- | --- |
| `error: could not run tailscale: [Errno 2] No such file or directory: 'tailscale'` | **No tailnet address** — Tailscale is not installed. `install.sh` does not check for it. | Install Tailscale, `sudo tailscale up`, confirm `tailscale ip -4` prints an address, then `sudo systemctl restart harbor-console-web`. |
| `error: tailscale ip -4 exited 1` | Tailscale is installed but logged out or not running. | `sudo systemctl start tailscaled`, `sudo tailscale up`. |
| `error: tailscale ip -4 returned no address` | Running, but this node holds no IPv4 address yet. | Wait for it to come up, or re-authenticate the node. |
| `error: tailscale ip -4 did not answer within 5.0s` | The binary hung. The timeout is deliberate: a hang with no timeout would leave the unit "starting" forever with nothing in the journal. | Investigate `tailscaled`; restarting it usually clears it. |
| `error: /opt/harbor-console/services.toml: ...` | **Unreadable ledger** — the file exists but is not valid TOML, a lease is missing a field, a lease has a bad `port` or `granted` date, or the same `(host, addr, port)` is claimed twice. The message names the file and the fault. A *missing* `services.toml` does **not** land here — `load_leases` treats a missing file as an empty ledger, which surfaces below as "not declared" instead. | Fix `services.toml` in your checkout, re-run `sudo deploy/install.sh`. Do not hand-edit the deployed copy; the next install overwrites it. |
| `error: no lease for harbor-console/web; this service must be declared` | **Not declared** — the ledger carries no lease for this service. This is also what a completely missing `services.toml` produces, since a missing file loads as an empty ledger rather than an error. A page bound to a port no lease reserves is exactly the collision the ledger exists to prevent, so there is no default port. | Declare it in `.harbor.toml`, run `harbor-console ports sync` on the dev box, re-run the installer. |
| `error: 2 leases for harbor-console/web, on hpz440 (0.0.0.0:8090), other (0.0.0.0:8090); running on more than one host needs an explicit choice of identity, which this service does not have` | **Declared more than once.** The ledger is fleet-wide, so two machines may each legitimately declare it; picking the first would bind a port this host may not hold and label the page a machine it is not. There is no hostname tiebreak on purpose. | Leave exactly one `harbor-console`/`web` lease in `services.toml` until multi-host operation is designed. |
| `error: could not bind 100.69.239.123:8090: [Errno 98] Address already in use` | Something else holds the leased port — often a previous instance that has not exited, or an undeclared container. | `sudo ss -ltnp \| grep 8090` to find the holder. |
| `error: could not bind 100.69.239.123:8090: [Errno 99] Cannot assign requested address` | The Tailscale address answered but is not on this machine's interfaces yet — usually a race just after boot. Systemd's retry normally resolves it. | If it persists, check `ip -4 addr show tailscale0`. |

### The page is up but says something is wrong

These are findings, not faults in the service:

- **`LISTENING`** next to a service means something holds the leased port but
  did not answer an HTTP probe. That is the expected state for a service that
  does not speak HTTP, such as `ice-colder/mqtt` on 1883. It is not an error.
- **`DOWN`** means a leased port with nothing listening on it at all.
- **`UNKNOWN`** means the first probe cycle has not completed yet. The prober
  runs every 30s in a background thread, never inside a request handler.
- **`/ports.json` answering 503** is deliberate, not a failure: an unprobed
  snapshot, or one collected while Docker was unreachable, cannot be served as
  a verified answer to the allocator. The body says which. It clears on its own
  once a full cycle succeeds.

## Smoke test (run once on the target)

Steps 1–5 cover the dashboard; 6–11 cover the status page and the two v0.2.0
release criteria that need a live host to verify.

1. Validate both installed units:
   `systemd-analyze verify /etc/systemd/system/harbor-console.service`
   and `systemd-analyze verify /etc/systemd/system/harbor-console-web.service`
   → no output, exit 0 for each.
2. Reboot → the dashboard appears on the attached monitor (tty1).
3. Confirm the Docker container count is correct (not stuck at 0) — verifies the
   `harbor` user's `docker`-group access.
4. `sudo systemctl kill harbor-console` → the dashboard returns within ~2s.
5. Press Ctrl+Alt+F2 → a normal login prompt is available.
6. `systemctl is-active harbor-console-web` → `active`, and
   `journalctl -u harbor-console-web -b` ends with one
   `harbor-console-web listening on http://<tailscale ip>:<leased port>/` line.

### 7. Reachable from anywhere on the tailnet

From a **different** machine on the tailnet:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://hpz440:8090/    # → 200
```

MagicDNS resolves the name, which is why there is no discovery protocol. If
the name does not resolve, the Tailscale IPv4 address works the same way.

### 8. Reachable from nowhere else — the one that matters

Binding is the whole access-control model, and a broader bind fails *silently*:
the page still works, it is simply visible to everyone. Verify it two ways.

On the host, check what is actually bound:

```bash
sudo ss -ltnp | grep 8090
```

→ the local address must be the Tailscale address (`100.x.y.z:8090`), **never**
`0.0.0.0:8090` and never `*:8090`.

Then, from a machine on the same LAN that is **not** on the tailnet, using the
host's LAN address (not its tailnet address or MagicDNS name):

```bash
curl -sS --connect-timeout 5 http://192.0.2.10:8090/
```

→ must fail with `Connection refused` (or time out). Anything that returns a
page is a failure of this test: stop `harbor-console-web` and find out why it
bound something broader before putting it back.

### 9. Both units survive each other

Neither process may disturb the other.

```bash
systemctl show -p MainPID --value harbor-console       # note the PID
sudo systemctl restart harbor-console-web
systemctl show -p MainPID --value harbor-console       # unchanged
```

→ the dashboard on tty1 does not blink, and its PID is the same.

```bash
systemctl show -p MainPID --value harbor-console-web   # note the PID
sudo systemctl kill harbor-console                     # the dashboard restarts within ~2s
curl -sS -o /dev/null -w '%{http_code}\n' http://hpz440:8090/   # → 200, uninterrupted
systemctl show -p MainPID --value harbor-console-web   # unchanged
```

Logging in at tty1, or logging out again, must likewise leave the page
untouched.

### 10. The page loads promptly even when a declared service is hung

Probing runs in a background thread, so a hung service can never slow a
request:

```bash
time curl -sS -o /dev/null http://hpz440:8090/
```

→ well under a second, even while a declared service is not answering.

### 11. Removal

`sudo deploy/uninstall.sh` → the login prompt returns on tty1, and both
`systemctl is-active harbor-console` and
`systemctl is-active harbor-console-web` report `inactive`.
