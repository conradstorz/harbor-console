"""Rendering the status page, and serving it.

Renders a snapshot and nothing else -- it never collects and never probes. One
hung service must not make the page slow to load, which is why probing happens
in a background thread and the handler only reads the last published snapshot.

The page is read-only: no forms, no buttons, no state-changing routes. Port
allocation authority was chosen over lifecycle control, so nothing here can
start or stop a container.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from html import escape
from http.server import BaseHTTPRequestHandler

from harbor_console.ports.keys import addrs_overlap
from harbor_console.snapshot import Snapshot

REFRESH_SECONDS = 30

_STYLE = """
body { font-family: ui-monospace, monospace; margin: 2rem; max-width: 60rem; }
h1, h2 { font-weight: 600; }
table { border-collapse: collapse; width: 100%; margin-bottom: 2rem; }
td, th { text-align: left; padding: 0.25rem 0.75rem 0.25rem 0; vertical-align: top; }
tr.detail td { padding-left: 2rem; opacity: 0.75; }
.down { font-weight: 700; }
.banner { border: 1px solid; padding: 0.5rem 0.75rem; margin-bottom: 1.5rem; }
.stamp { opacity: 0.7; }
"""


def ports_payload(snapshot: Snapshot) -> dict:
    """Build the /ports.json body the allocator reads.

    Container attribution is by (addr, port) against Docker's published ports,
    not by PID: running unprivileged we cannot see another user's process, and
    container processes are never ours. The fallback match uses
    `ports.keys.addrs_overlap`, the same address-overlap rule the ledger and
    `ports/live.py` use, so a wildcard listener matches a specific publish and
    vice versa -- reimplementing that comparison here would drift from it.
    """
    owners: dict[tuple[str, int], str] = {}
    for container in snapshot.containers:
        for addr, port in container.published:
            owners[(addr, port)] = container.name

    listening = []
    for listener in snapshot.listeners:
        container = owners.get((listener.addr, listener.port))
        if container is None:
            for (owner_addr, owner_port), name in owners.items():
                if owner_port == listener.port and addrs_overlap(owner_addr, listener.addr):
                    container = name
                    break
        listening.append(
            {"addr": listener.addr, "port": int(listener.port), "container": container}
        )

    return {
        "host": str(snapshot.metrics["hostname"]),
        "collected": snapshot.collected.isoformat(),
        "listening": listening,
    }


def render_page(snapshot: Snapshot) -> bytes:
    """Render the whole status page as one self-contained document."""
    parts = [
        "<!doctype html><html><head><meta charset=\"utf-8\">",
        f"<meta http-equiv=\"refresh\" content=\"{REFRESH_SECONDS}\">",
        "<title>Harbor Console</title>",
        f"<style>{_STYLE}</style></head><body>",
        f"<h1>{escape(str(snapshot.metrics['hostname']))}</h1>",
    ]

    if snapshot.collection_error is not None:
        parts.append(
            f"<p class=\"banner\">The last collection cycle failed: "
            f"{escape(snapshot.collection_error)}. Showing the last good page.</p>"
        )
    if not snapshot.docker_available:
        parts.append(
            "<p class=\"banner\">Docker could not be read, so undeclared containers "
            "and port mismatches are not reported.</p>"
        )

    parts.append(_host_table(snapshot))
    parts.append(_services_table(snapshot))
    parts.append(_drift_section(snapshot))
    parts.append(
        f"<p class=\"stamp\">Collected "
        f"{escape(snapshot.collected.strftime('%Y-%m-%d %H:%M:%S'))}, "
        f"refreshing every {REFRESH_SECONDS}s.</p>"
    )
    parts.append("</body></html>")
    return "".join(parts).encode("utf-8")


def _host_table(snapshot: Snapshot) -> str:
    rows = [
        ("Uptime", snapshot.metrics["uptime"]),
        ("CPU", f"{float(snapshot.metrics['cpu_utilization']):.1f}%"),
        ("Memory", f"{float(snapshot.metrics['memory_utilization']):.1f}%"),
        ("Disk", f"{float(snapshot.metrics['disk_utilization']):.1f}%"),
        ("IPv4", snapshot.metrics["ipv4_address"]),
        ("Containers", snapshot.metrics["docker_container_count"]),
        ("Time", snapshot.metrics["current_datetime"]),
    ]
    cells = "".join(
        f"<tr><td>{escape(label)}</td><td>{escape(str(value))}</td></tr>"
        for label, value in rows
    )
    return f"<h2>Host</h2><table>{cells}</table>"


def _services_table(snapshot: Snapshot) -> str:
    """Render the directory, saying "unknown" until something has been probed.

    Before the first cycle the health map is empty, and reading that as DOWN
    would report the whole fleet dead on the strength of having looked at
    none of it. The leases are real either way -- they come from the ledger,
    not from a probe -- so the directory is still shown.
    """
    if not snapshot.leases:
        return "<h2>Services</h2><p>No services are declared.</p>"

    rows = []
    for lease in snapshot.leases:
        health = snapshot.health.get((lease.project, lease.name))
        up = health is not None and health.up
        url = f"http://{lease.host}:{lease.port}/"
        if not snapshot.probed:
            status = "UNKNOWN"
        else:
            status = "UP" if up else "<span class=\"down\">DOWN</span>"
        summary = escape(health.summary) if health and health.summary else ""
        rows.append(
            f"<tr><td>{escape(lease.project)}/{escape(lease.name)}</td>"
            f"<td><a href=\"{escape(url)}\">{escape(lease.addr)}:{lease.port}</a></td>"
            f"<td>{status}</td><td>{summary}</td></tr>"
        )
        if health is not None:
            for row in health.detail:
                rows.append(
                    f"<tr class=\"detail\"><td colspan=\"4\">{escape(row.label)}: "
                    f"{escape(row.value)}</td></tr>"
                )
            if health.warning:
                rows.append(
                    f"<tr class=\"detail\"><td colspan=\"4\">{escape(health.warning)}</td></tr>"
                )

    note = (
        ""
        if snapshot.probed
        else "<p>Nothing has been collected yet: the first cycle has not "
        "completed, so service state is unknown.</p>"
    )
    return (
        "<h2>Services</h2>"
        + note
        + "<table>"
        "<tr><th>Project</th><th>Address</th><th>State</th><th></th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _drift_section(snapshot: Snapshot) -> str:
    """Report drift, distinguishing "none found" from "nothing looked at".

    An empty tuple means both, and only one of them is good news.
    """
    if not snapshot.probed:
        return (
            "<h2>Drift</h2><p>Nothing has been collected yet: the first "
            "cycle has not completed, so drift is unknown.</p>"
        )
    if not snapshot.drift:
        return "<h2>Drift</h2><p>No drift: every lease matches what is running.</p>"

    items = "".join(
        f"<li>{escape(item.kind)} &mdash; {escape(item.detail)}</li>" for item in snapshot.drift
    )
    return f"<h2>Drift</h2><ul>{items}</ul>"


def make_handler(get_snapshot: Callable[[], Snapshot]) -> type[BaseHTTPRequestHandler]:
    """Build a handler class that reads the latest snapshot and nothing else."""

    class Handler(BaseHTTPRequestHandler):
        """Serves the page and /ports.json. Read-only; no other route exists."""

        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib's required name
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", render_page(get_snapshot()))
            elif self.path == "/ports.json":
                body = json.dumps(ports_payload(get_snapshot())).encode("utf-8")
                self._send(200, "application/json", body)
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found\n")

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            """Quiet by default; journald already timestamps what matters."""

    return Handler
