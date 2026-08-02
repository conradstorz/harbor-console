# Architecture

Harbor Console MVP is intentionally simple:

- `system.py` collects all runtime metrics.
- `ui.py` only renders the dashboard from provided metrics.
- `app.py` runs a 1-second refresh loop and exits cleanly on `Ctrl+C`.

The reasoning behind these and other choices is recorded as Architecture
Decision Records in [`adr/`](adr/README.md).
