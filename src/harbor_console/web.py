"""Rendering the status page, and serving it.

Renders a snapshot and nothing else -- it never collects and never probes.
Probing happens in a background thread and the handler only reads the last
published snapshot, so one hung service cannot make the page slow.

The page is read-only: no forms, no buttons, no state-changing routes.
"""

from __future__ import annotations

from collections.abc import Callable
from html import escape
from http.server import BaseHTTPRequestHandler

from harbor_console.directory import KIND_HTTP, STATE_DOWN, STATE_ROUTE_ERROR, Row
from harbor_console.inventory import REACH_LOOPBACK, Entry
from harbor_console.snapshot import Snapshot

REFRESH_SECONDS = 30

_STYLE = """
body { font-family: ui-monospace, monospace; margin: 2rem; max-width: 60rem; }
h1, h2 { font-weight: 600; }
table { border-collapse: collapse; width: 100%; margin-bottom: 2rem; }
td, th { text-align: left; padding: 0.25rem 0.75rem 0.25rem 0; vertical-align: top; }
tr.detail td { padding-left: 2rem; opacity: 0.75; }
.down { font-weight: 700; }
.unaccounted { font-weight: 700; }
.banner { border: 1px solid; padding: 0.5rem 0.75rem; margin-bottom: 1.5rem; }
.stamp { opacity: 0.7; }
"""


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
        tail = " Showing the last good page." if snapshot.probed else ""
        parts.append(
            f"<p class=\"banner\">The last collection cycle failed: "
            f"{escape(snapshot.collection_error)}.{tail}</p>"
        )
    if not snapshot.docker_available:
        parts.append(
            "<p class=\"banner\">Docker could not be read, so undeclared containers "
            "and bypassed routes are not reported.</p>"
        )
    if not snapshot.traefik_available:
        parts.append(
            "<p class=\"banner\">Traefik could not be read, so route errors are not "
            "reported and HTTP rows show only what the probe saw.</p>"
        )
    if not snapshot.listeners_available:
        parts.append(
            "<p class=\"banner\">The host's listening sockets could not be read, so "
            "the inventory below is missing and undeclared tailnet listeners are not "
            "reported.</p>"
        )
    parts.append(_host_table(snapshot))
    parts.append(_directory_table(snapshot))
    parts.append(_findings_section(snapshot))
    parts.append(_inventory_section(snapshot))
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
    if snapshot.tailnet_address is not None:
        rows.insert(5, ("Tailnet", snapshot.tailnet_address))
    cells = "".join(
        f"<tr><td>{escape(label)}</td><td>{escape(str(value))}</td></tr>"
        for label, value in rows
    )
    return f"<h2>Host</h2><table>{cells}</table>"


def _target_cell(row: Row) -> str:
    if row.kind == KIND_HTTP and row.target:
        return f"<a href=\"{escape(row.target)}\">{escape(row.target)}</a>"
    return escape(row.target)


def _state_cell(row: Row) -> str:
    if row.state in (STATE_DOWN, STATE_ROUTE_ERROR):
        return f"<span class=\"down\">{escape(row.state)}</span>"
    return escape(row.state)


def _directory_table(snapshot: Snapshot) -> str:
    if not snapshot.probed:
        return (
            "<h2>Directory</h2><p>Nothing has been collected yet: the first cycle "
            "has not completed, so the directory is unknown.</p>"
        )
    if not snapshot.rows:
        return "<h2>Directory</h2><p>No services are declared.</p>"
    rows = []
    for row in snapshot.rows:
        rows.append(
            f"<tr><td>{escape(row.name)}</td><td>{escape(row.kind)}</td>"
            f"<td>{_target_cell(row)}</td><td>{_state_cell(row)}</td>"
            f"<td>{escape(row.container)}</td><td>{escape(row.description)}</td></tr>"
        )
        # `health` is keyed by router name, which only an HTTP row carries;
        # a tcp or internal container of the same name must not inherit it.
        health = snapshot.health.get(row.name) if row.kind == KIND_HTTP else None
        if health is not None:
            if health.summary:
                rows.append(f"<tr class=\"detail\"><td colspan=\"6\">{escape(health.summary)}</td></tr>")
            for detail in health.detail:
                rows.append(
                    f"<tr class=\"detail\"><td colspan=\"6\">{escape(detail.label)}: "
                    f"{escape(detail.value)}</td></tr>"
                )
            if health.warning:
                rows.append(f"<tr class=\"detail\"><td colspan=\"6\">{escape(health.warning)}</td></tr>")
    return (
        "<h2>Directory</h2><table>"
        "<tr><th>Name</th><th>Kind</th><th>Where</th><th>State</th><th>Container</th><th></th></tr>"
        + "".join(rows) + "</table>"
    )


def _findings_section(snapshot: Snapshot) -> str:
    if not snapshot.probed:
        return (
            "<h2>Findings</h2><p>Nothing has been collected yet: the first cycle "
            "has not completed, so findings are unknown.</p>"
        )
    if not snapshot.findings:
        return (
            "<h2>Findings</h2><p>No findings: every container is declared and "
            "every route is live.</p>"
        )
    items = "".join(
        f"<li>{escape(item.kind)} &mdash; {escape(item.detail)}</li>" for item in snapshot.findings
    )
    return f"<h2>Findings</h2><ul>{items}</ul>"


def _where_cell(entry: Entry) -> str:
    """`addr:port`, bracketing IPv6 so the port is still legible."""
    if ":" in entry.addr:
        return f"[{entry.addr}]:{entry.port}"
    return f"{entry.addr}:{entry.port}"


def _accounted_cell(entry: Entry) -> str:
    if entry.accounted:
        return escape(entry.accounted)
    return "<span class=\"unaccounted\">&mdash;</span>"


def _inventory_table(entries: tuple[Entry, ...]) -> str:
    rows = "".join(
        f"<tr><td>{escape(entry.proto)}</td><td>{escape(_where_cell(entry))}</td>"
        f"<td>{escape(entry.reach)}</td><td>{_accounted_cell(entry)}</td></tr>"
        for entry in entries
    )
    return (
        "<table><tr><th>Proto</th><th>Address:Port</th><th>Reach</th>"
        "<th>Accounted for</th></tr>" + rows + "</table>"
    )


def _inventory_section(snapshot: Snapshot) -> str:
    """Every listening socket, reachable ones first.

    This is the reference, not the alert: the findings above are what
    disagrees, and this is what is there. Loopback is split out rather than
    dropped -- it cannot be reached from off the host, but it is still the
    answer to "what is running".
    """
    if not snapshot.probed:
        return (
            "<h2>Listening</h2><p>Nothing has been collected yet: the first cycle "
            "has not completed, so what is listening is unknown.</p>"
        )
    if not snapshot.listeners_available:
        return (
            "<h2>Listening</h2><p>The host's listening sockets could not be read, "
            "so what is listening is unknown.</p>"
        )
    reachable = tuple(e for e in snapshot.inventory if e.reach != REACH_LOOPBACK)
    loopback = tuple(e for e in snapshot.inventory if e.reach == REACH_LOOPBACK)
    parts = ["<h2>Listening</h2>"]
    if reachable:
        parts.append(_inventory_table(reachable))
    else:
        parts.append("<p>Nothing is listening on a reachable address.</p>")
    if loopback:
        parts.append("<h2>Loopback only</h2>")
        parts.append(_inventory_table(loopback))
    return "".join(parts)


def make_handler(get_snapshot: Callable[[], Snapshot]) -> type[BaseHTTPRequestHandler]:
    """Build a handler class that reads the latest snapshot and nothing else."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib's required name
            try:
                self._dispatch()
            except Exception:  # noqa: BLE001 - a request boundary
                self._send(500, "text/plain; charset=utf-8", b"internal error\n")

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib's required name
            """Same headers as GET, no body: what `curl -I` and uptime checks send."""
            self.do_GET()

        def _dispatch(self) -> None:
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", render_page(get_snapshot()))
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found\n")

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            """Quiet by default; journald already timestamps what matters."""

    return Handler
