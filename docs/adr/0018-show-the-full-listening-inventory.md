# 0018. Show the full listening inventory, not only what a rule anticipates

Date: 2026-09-21

## Status

Accepted

## Context

The status page reported four findings and rendered nothing else its
collectors gathered. `Snapshot.listeners` carried every listening socket on
the host and no template touched it, so the only path from a socket to a
reader was a finding rule firing.

One of those rules selected its candidates by comparing bind addresses with
`==` against the host's tailnet address. A process bound to `0.0.0.0` answers
on that address and was skipped, which hid every wildcard bind on the host --
sshd among them. A test, `test_a_wildcard_listener_is_not_a_tailnet_finding`,
asserted exactly that behaviour, so the blind spot was not merely unnoticed:
it was pinned.

The collector also asked psutil for `kind="tcp"`, so no UDP socket was ever
seen. On this host that hides tailscaled's WireGuard port, itself a wildcard
bind, along with the resolver and both DHCP clients.

A port scan of 1024-65535 was considered. It sees strictly less than the
kernel's own socket table -- no PID, no loopback-only listener, no UDP -- and
would have had nowhere to be displayed.

## Decision

We will show every listening socket on the page: protocol, address and port,
who can reach it, and what accounts for it, split into a reachable table and a
loopback-only table. A new pure module, `inventory.py`, does that
classification; `listening.py` collects UDP alongside TCP; and the finding
rule compares addresses with `addrs_overlap` rather than `==`, which makes a
wildcard bind nothing publishes a finding. The test that asserted the old
behaviour is replaced by its inverse.

UDP generates no findings. Reachability is a column, not a rule: a service
bound to `0.0.0.0` is reported as `LAN + tailnet` and nothing more, because
`harbor.kind=tcp` says nothing about bind address and such a container
violates no declaration of its own.

## Consequences

The page answers "what might this host respond to" by inspection rather than
by trusting that a rule anticipated the question. A filter applied before
anyone can look is unfalsifiable, which is how the wildcard comparison
survived; a table is checkable against `ss -tuln` by anyone who doubts it.

`0.0.0.0:22` becomes a standing finding. It is true -- sshd is reachable on
the tailnet and no container publishes it -- and there is deliberately no
allowlist to silence it, because an allowlist would be the configuration file
this project does not have.

Attribution is partial: the service runs unprivileged and psutil cannot map
socket inode to PID for another user's socket, so most host daemons show no
owner. Raising privilege to fill the column in was rejected as spending the
hardening of [ADR 16](0016-close-the-harbor-network-attack-surface.md) on a
label. An unattributed row is the signal.

That `ice-colder-mqtt` binds `0.0.0.0` and so answers on the whole LAN, while
every other service is tailnet-only, is now visible. Whether that is
acceptable is a separate decision, deferred to its own ADR rather than
smuggled in as a finding here.
