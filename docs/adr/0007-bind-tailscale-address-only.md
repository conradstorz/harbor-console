# 7. Bind the web status page to the Tailscale address only

Date: 2026-09-01

## Status

Accepted

## Context

[ADR 6](0006-service-registry-and-web-status-page.md) adds
`harbor-console-web`, a page that lists every service on the host with its port
and whether it is up. That page is a map of the attack surface of the machine.
It is exactly what an intruder on the LAN would want to read first, and it is
also the kind of page that is convenient to leave open.

Access control options considered: a login page, an allowlist by source
address, a reverse proxy in front, or binding.

The host is already on a tailnet. Tailscale membership is an existing,
maintained authentication boundary — devices are enrolled, revocable, and
audited by someone other than this project. Any credential scheme written here
would be weaker than the one already in place, and would need storage, rotation
and a password reset story that this project has no business owning.

The failure mode that matters is not "someone breaks the login." It is a
process that cannot reach its intended interface and helpfully falls back to
something broader, publishing the inventory to the whole LAN with no error
raised. Fallbacks of that shape are usually written as a convenience during
development and never removed.

## Decision

We will bind `harbor-console-web` to the host's Tailscale address only, and
never to `0.0.0.0`.

If the Tailscale address cannot be determined, the service **refuses to start**
and exits non-zero. systemd retries it (`After=tailscaled.service`,
`Restart=always`). There is no fallback bind, no `--host` override, and no
"development mode" that relaxes this.

There is no login page. The network is the gate.

## Consequences

- Reachable from any enrolled device anywhere, and from nothing else. No
  credentials to manage, rotate, or leak.
- The page is unreachable from the LAN, including from the host's own LAN
  address. That is intended, and it means "I can't load it" is a normal
  diagnosis with a normal answer: check whether Tailscale is up.
- Boot ordering becomes a real dependency. The service may flap at startup
  until `tailscaled` has an address, and the unit must tolerate that rather
  than hide it.
- The refuse-to-start behaviour is a testable requirement, not a comment: a
  test must assert that an unresolvable Tailscale address produces a failure
  and not a broader bind.
- If the page ever needs to be readable from outside the tailnet, that is a new
  decision requiring a new ADR — not a flag on this one.
- This decision is why the page can be read-only and unauthenticated at the
  same time. If write actions are ever added, the whole access-control
  reasoning here must be reopened.
