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
