# 3. No plugins (and no configuration) in the MVP

Date: 2026-08-01

## Status

Accepted — the no-configuration-file aspect is amended by
[ADR 6](0006-service-registry-and-web-status-page.md), which adds a declared
`services.toml` registry. The no-plugins, no-themes, no-user-configuration
stance stands.

## Context

`founding_document.txt` defines a deliberately minimal MVP (v0.1.0) whose success
criterion is answering "Is my server healthy?" in under five seconds. A long list
of capabilities — plugin architecture, themes, config files, YAML, interactive
menus, service/Docker management, remote monitoring — is explicitly deferred
"until a real operational need is demonstrated."

A plugin/config system is the kind of infrastructure that is tempting to add
early and hard to remove later, and it would enlarge the surface area well beyond
the MVP's single question.

## Decision

Ship the MVP with a fixed, hard-coded set of metrics and no extensibility layer:
no plugins, no configuration files, no themes, no keyboard shortcuts. Metrics are
collected by `system.py` and rendered by `ui.py` directly.

## Consequences

- The codebase stays at three small modules (`system`, `ui`, `app`) that any
  contributor can read in minutes.
- Behaviour changes require code changes, which is acceptable at this scale and
  keeps "main is always deployable" easy to uphold.
- Adding a deferred capability later should be recorded as a new ADR that
  supersedes or amends this one, so the reason for the reversal is captured.
- Testability is preserved instead via dependency injection in `app.run()`
  (injectable collector/renderer/sleep), not via a plugin system.
