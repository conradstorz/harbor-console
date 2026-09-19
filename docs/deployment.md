# Deploying Harbor Console

Harbor Console deploys as **two** systemd units from one checkout, one
virtualenv and one service user, plus the edge it owns — Traefik, as a
container:

| Unit | What it does | Where you see it |
| --- | --- | --- |
| `harbor-console.service` | Takes over the physical console (`tty1`) at boot and shows the dashboard. | The attached monitor |
| `harbor-console-web.service` | Serves the read-only status page to the tailnet, on fixed port 8100. | `https://harbor.hpz440.ohr3023.org/` from any tailnet peer (direct: `http://<tailnet ip>:8100/`) |
| Traefik (container) | Fronts every HTTP service on the host, on the tailnet address's `:80` and `:443`. | Every `https://<name>.hpz440.ohr3023.org/` route |

The two units are installed, enabled and restarted together — a partial deploy
is a state nobody wants to debug — but they have **independent lifetimes**:
neither can take the other down, and neither depends on Traefik to keep
running. This guide covers install, verification, diagnosis, update and removal.

## Prerequisites

- A systemd-based Linux host.
- Root access (`sudo`).
- [`uv`](https://docs.astral.sh/uv/) and `rsync` installed on the host.
  `install.sh` fails fast without either.
- Docker, with the `compose` plugin — the service user joins the `docker`
  group, both units set `SupplementaryGroups=docker`, and the installer starts
  Traefik with `docker compose`. `install.sh` fails fast if the `docker` group
  does not exist.
- **Tailscale installed, running, and holding an IPv4 address.** Check with:

  ```bash
  tailscale ip -4     # must print one IPv4 address, e.g. 100.69.239.123
  ```

  `harbor-console-web` binds that address and only that address, and Traefik
  publishes that address and only that address. There is no fallback to
  `0.0.0.0`, no `--host`, and no dev mode — binding *is* the access control
  ([ADR 7](adr/0007-bind-tailscale-address-only.md),
  [ADR 15](adr/0015-reverse-proxy-and-label-declared-services.md)). The
  installer needs the address to render Traefik's configuration, so it fails
  fast when `tailscale` is missing or prints nothing.

- **`/etc/traefik/env`, mode 0600**, holding the Cloudflare token that issues
  the wildcard certificate and the address Let's Encrypt notices go to. It
  never lives in the repository:

  ```
  CF_DNS_API_TOKEN=<cloudflare token with Zone.DNS edit on ohr3023.org>
  ACME_EMAIL=<address for Let's Encrypt notices>
  ```

- **A DNS record** at Cloudflare: `*.hpz440` A → `100.69.239.123`, **proxy
  off**. The address is tailnet-only, so the public record leads nowhere for
  anyone who is not on the tailnet; it exists so the DNS-01 challenge and the
  route names resolve.

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
- Prepares the edge: creates the external `harbor` Docker network if it is
  missing, renders `deploy/traefik/.env` and `deploy/traefik/dynamic/harbor.yml`
  from the host's tailnet address, and brings Traefik up with
  `docker compose up -d`.
- Installs `/etc/systemd/system/harbor-console.service` **and**
  `/etc/systemd/system/harbor-console-web.service`.
- Masks `getty@tty1.service` (removes the login prompt on **tty1 only**).
- Enables and restarts **both** units, then prints `systemctl status` for each.

## The edge (Traefik)

Traefik is deployed from `deploy/traefik/compose.yaml` in this repository, so
the host's edge is versioned with everything else.

- It publishes `100.69.239.123:80` and `100.69.239.123:443` — the tailnet
  address only, never `0.0.0.0`. Port 80 does nothing but redirect to 443.
- Its API and dashboard are on an entrypoint bound to `127.0.0.1:8081`. That is
  where `harbor-console-web` reads the routers; nothing off the host can reach
  it.
- Its Docker provider runs with `exposedByDefault=false` on the external
  `harbor` network. **A container that is not on that network is not routed**,
  which the status page reports as a `route-error` finding.
- One wildcard certificate for `*.hpz440.ohr3023.org`, issued over a Cloudflare
  DNS-01 challenge using the token in `/etc/traefik/env`, and persisted in a
  named `letsencrypt` volume. The volume is deliberately not removed on
  uninstall: the certificate is expensive to re-issue.
- One file-provider route, rendered by the installer from
  `dynamic/harbor.yml.in`: `harbor.hpz440.ohr3023.org` →
  `http://<tailnet ip>:8100`. The status page is a systemd process, not a
  container, so it carries no labels; that file is its declaration.
- Traefik's own container carries `harbor.kind=edge`, a kind reserved for the
  proxy, so the page neither calls it undeclared nor reports it for publishing
  80 and 443.

A service joins the edge by adding labels and the `harbor` network to its own
compose file — see [Declaring a service](../README.md#declaring-a-service).
Nothing in this repository has to change when a service appears.

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

It re-syncs `/opt/harbor-console`, rebuilds the virtualenv with `uv sync`,
re-renders Traefik's configuration and brings it up, and restarts both services
so the new code takes effect. Note: `/opt/harbor-console` is a copy, not a git
clone — pull updates in your checkout, then re-run the installer.

## Uninstall

```bash
sudo deploy/uninstall.sh          # stops Traefik and both units, restores the tty1 login prompt
sudo deploy/uninstall.sh --purge  # also removes /opt/harbor-console and the harbor user
```

Both units are disabled, stopped and removed, Traefik is brought down, and
`getty@tty1` is unmasked and started. The `harbor` network and the
`letsencrypt` volume are left in place — other projects join that network, and
re-issuing the certificate is not free. Without `--purge`,
`/opt/harbor-console` and the `harbor` user are left in place too.

## Checking status and logs

Every command takes a unit name, so run the pair:

```bash
systemctl status harbor-console          # the tty1 dashboard
systemctl status harbor-console-web      # the tailnet status page

journalctl -u harbor-console -b          # this boot's logs, dashboard
journalctl -u harbor-console-web -b      # this boot's logs, status page
journalctl -u harbor-console-web -f      # follow, e.g. while diagnosing a crash loop
```

The edge logs to Docker:

```bash
docker logs --tail 50 traefik
docker compose -f /opt/harbor-console/deploy/traefik/compose.yaml ps
```

A healthy `harbor-console-web` logs exactly one line per start:

```
harbor-console-web listening on http://100.69.239.123:8100/
```

Request logging is deliberately off — journald already timestamps what matters.
So after that line, silence is the normal state.

Confirm what it actually bound:

```bash
sudo ss -ltnp | grep ':8100 '
```

The local address must be the Tailscale address. **If it ever shows `0.0.0.0`,
something is very wrong** — the page is an inventory of every service on the
host, and `0.0.0.0` publishes it to the whole LAN. The same is true of
Traefik's `:80` and `:443`:

```bash
sudo ss -ltnp | grep -E ':(80|443) '
```

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

`harbor-console-web` has **one startup refusal** — no tailnet address — plus a
bind that can fail. Each happens before anything is served, prints one `error:`
line to the journal, and exits 1. `Restart=always` with `RestartSec=2` and
`StartLimitIntervalSec=0` means systemd retries every two seconds indefinitely
and never marks the unit failed — so the symptom is always the same:
`systemctl status harbor-console-web` shows `activating (auto-restart)` and a
climbing restart count, and the reason is only in the journal.

Read the reason first:

```bash
journalctl -u harbor-console-web -b | grep '^.*error:'
```

| Journal line | What it means | Fix |
| --- | --- | --- |
| `error: could not run tailscale: [Errno 2] No such file or directory: 'tailscale'` | **No tailnet address** — Tailscale is not installed. | Install Tailscale, `sudo tailscale up`, confirm `tailscale ip -4` prints an address, then `sudo systemctl restart harbor-console-web`. |
| `error: tailscale ip -4 exited 1` | Tailscale is installed but logged out or not running. | `sudo systemctl start tailscaled`, `sudo tailscale up`. |
| `error: tailscale ip -4 returned no address` | Running, but this node holds no IPv4 address yet. | Wait for it to come up, or re-authenticate the node. |
| `error: tailscale ip -4 did not answer within 5.0s` | The binary hung. The timeout is deliberate: a hang with no timeout would leave the unit "starting" forever with nothing in the journal. | Investigate `tailscaled`; restarting it usually clears it. |
| `error: could not bind 100.69.239.123:8100: [Errno 98] Address already in use` | Something else holds port 8100 — often a previous instance that has not exited, or a container publishing it. The port is fixed, because Traefik's route points at it, so the holder has to move rather than the page. | `sudo ss -ltnp \| grep ':8100 '` to find the holder. |
| `error: could not bind 100.69.239.123:8100: [Errno 99] Cannot assign requested address` | The Tailscale address answered but is not on this machine's interfaces yet — usually a race just after boot. Systemd's retry normally resolves it. | If it persists, check `ip -4 addr show tailscale0`. |

Traefik has its own failure modes, and they never take the page down:

- `docker logs traefik` reporting an ACME error usually means the Cloudflare
  token in `/etc/traefik/env` is wrong or lacks `Zone.DNS` edit on the zone.
- A route that 404s at the proxy is a router Traefik never accepted; the status
  page names it under `route-error`, with Traefik's own reason.

### The page is up but says something is wrong

These are findings, not faults in the service:

- **`ROUTE ERROR`** next to a row means Traefik rejected that router, or never
  saw it — most often a container that declares a route but is not on the
  `harbor` network, or a `harbor.port` naming a port the container does not
  publish. For an HTTP row the Findings list carries Traefik's own reason.
- **`DOWN`** on an HTTP row means the route exists but nothing answered
  through it; on a `tcp` or `edge` row it means nothing is listening on the
  declared port.
- **`LISTENING`** is the healthy state for a `tcp` row, such as
  `ice-colder-mqtt` on 1883: something holds the port, and no HTTP probe is
  attempted. **`INTERNAL`** is the healthy state for a container that
  deliberately publishes nothing.
- **`UNKNOWN`** means the first probe cycle has not completed yet. The prober
  runs every 30s in a background thread, never inside a request handler.
- **A banner** saying Docker or Traefik could not be read is the page telling
  you which findings it is *withholding* — absence of evidence is never
  reported as a finding. It clears on its own once a cycle succeeds.
- **`undeclared-container`** and **`bypasses-proxy`** are the migration
  checklist: a container with no labels at all, and one still publishing a host
  port that no `harbor.kind=tcp` label accounts for.

## Smoke test (run once on the target)

Steps 1–5 cover the dashboard; 6–11 cover the status page, the edge, and the
access-control criterion that needs a live host to verify.

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
   `harbor-console-web listening on http://<tailscale ip>:8100/` line.

### 7. Reachable from anywhere on the tailnet, through the proxy

From a **different** machine on the tailnet:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://harbor.hpz440.ohr3023.org/   # → 200
```

A real certificate, so no `-k`. If this fails but
`curl http://<tailscale ip>:8100/` works, the page is healthy and the edge is
not: check `docker logs traefik`.

### 8. Reachable from nowhere else — the one that matters

Binding is the whole access-control model, and a broader bind fails *silently*:
the page still works, it is simply visible to everyone. Verify it two ways.

On the host, check what is actually bound:

```bash
sudo ss -ltnp | grep -E ':(80|443|8100) '
```

→ every local address must be the Tailscale address (`100.x.y.z`), **never**
`0.0.0.0` and never `*`. The only exception is Traefik's API on
`127.0.0.1:8081`.

Then, from a machine on the same LAN that is **not** on the tailnet, using the
host's LAN address (not its tailnet address or MagicDNS name):

```bash
curl -sS --connect-timeout 5 http://192.0.2.10:8100/
curl -sS --connect-timeout 5 -k https://192.0.2.10/
```

→ both must fail with `Connection refused` (or time out). Anything that returns
a page is a failure of this test: stop the offending process and find out why it
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
curl -sS -o /dev/null -w '%{http_code}\n' https://harbor.hpz440.ohr3023.org/   # → 200, uninterrupted
systemctl show -p MainPID --value harbor-console-web   # unchanged
```

Logging in at tty1, or logging out again, must likewise leave the page
untouched.

### 10. The page loads promptly even when a declared service is hung

Probing runs in a background thread, so a hung service can never slow a
request:

```bash
time curl -sS -o /dev/null https://harbor.hpz440.ohr3023.org/
```

→ well under a second, even while a declared service is not answering.

### 11. Removal

`sudo deploy/uninstall.sh` → the login prompt returns on tty1, both
`systemctl is-active harbor-console` and
`systemctl is-active harbor-console-web` report `inactive`, and
`docker ps` no longer lists `traefik`.
