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
from harbor_console.ports.ledger import Lease
from harbor_console.snapshot import Snapshot

REFRESH_SECONDS = 30

#: Why `/ports.json` may refuse. Both windows are "we looked at less than the
#: whole host", and the allocator writes other repositories' `.env` files from
#: what this endpoint says, so both are refusals rather than a thin 200.
UNPROBED_REASON = "not yet probed: no collection cycle has completed"
DOCKER_REASON = "docker could not be read: container attribution is incomplete"

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


def _ports_refusals(snapshot: Snapshot) -> tuple[str, ...]:
    """Every reason `/ports.json` must refuse for this snapshot.

    Empty means the payload is answerable. Two conditions, not one: a snapshot
    nothing has been collected into yet, and one collected while Docker could
    not be read. The second is the half-blind window beside the first -- the
    sockets are real, but nothing can be attributed to a container, so a
    project already running on its wanted port looks unowned and the allocator
    reassigns it. The HTML page keeps serving in both windows; it reports the
    Docker outage in its own banner.
    """
    reasons = []
    if not snapshot.probed:
        reasons.append(UNPROBED_REASON)
    if not snapshot.docker_available:
        reasons.append(DOCKER_REASON)
    return tuple(reasons)


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
        if snapshot.probed:
            parts.append(
                f"<p class=\"banner\">The last collection cycle failed: "
                f"{escape(snapshot.collection_error)}. Showing the last good page.</p>"
            )
        else:
            # No cycle has ever completed, so there is no "last good page" to
            # show -- only the starting placeholder. Claiming one would sit
            # next to the "nothing has been collected yet" notes below and
            # contradict them.
            parts.append(
                f"<p class=\"banner\">The last collection cycle failed: "
                f"{escape(snapshot.collection_error)}.</p>"
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
        f"services.toml written {_ledger_written_text(snapshot)}, "
        f"refreshing every {REFRESH_SECONDS}s.</p>"
    )
    parts.append("</body></html>")
    return "".join(parts).encode("utf-8")


def _ledger_written_text(snapshot: Snapshot) -> str:
    """Render when the ledger this page serves was last written, or say so.

    `snapshot.ledger_written` is None when the ledger file is missing or
    unreadable, or before the first collection cycle -- both hostile
    conditions this project's collectors degrade on rather than raise.
    Rendering "unknown" in that case, instead of a bare `None`, is what makes
    a forgotten `install.sh` legible on the page rather than a silent gap.
    """
    if snapshot.ledger_written is None:
        return "unknown"
    return escape(snapshot.ledger_written.strftime("%Y-%m-%d %H:%M:%S"))


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


def _lease_has_listener(snapshot: Snapshot, lease: Lease) -> bool:
    """True when something on this host holds the lease's `(addr, port)`.

    Uses `ports.keys.addrs_overlap`, the same address-overlap rule the ledger,
    `ports/live.py` and `reconcile.py` join on, so a wildcard listener answers
    a specific lease and vice versa.

    `snapshot.listeners` is always local -- it comes from this machine's own
    sockets -- but `snapshot.leases` is fleet-wide, the same ledger every
    other host reads too. A lease belonging to another host must never be
    credited with a listener here: `reconcile.find_drift` already filters by
    `lease.host`, and this is that same filter, applied where the page
    decides LISTENING vs DOWN rather than where it decides drift. Without it,
    a lease on another host whose port happens to coincide with something
    listening here would read as LISTENING for a service that, on this host,
    is not running at all.
    """
    served_host = str(snapshot.metrics["hostname"])
    if lease.host != served_host:
        return False
    return any(
        listener.port == lease.port and addrs_overlap(listener.addr, lease.addr)
        for listener in snapshot.listeners
    )


def _services_table(snapshot: Snapshot) -> str:
    """Render the directory, saying "unknown" until something has been probed.

    Before the first cycle the health map is empty, and reading that as DOWN
    would report the whole fleet dead on the strength of having looked at
    none of it. The leases are real either way -- they come from the ledger,
    not from a probe -- so the directory is still shown.

    Three states, not two. The probe speaks only HTTP, and the ledger leases
    ports that do not: `ice-colder/mqtt` holds 1883 today. Calling a listening
    MQTT broker DOWN would print a red state directly under "No drift: every
    lease matches what is running", because `find_drift` joins on the listener
    and correctly finds nothing wrong. So a lease whose port is held but whose
    HTTP probe failed is LISTENING -- something is there, it just does not
    speak HTTP -- and DOWN is reserved for a lease with no listener at all,
    which is the finding this page exists to make.
    """
    if not snapshot.leases:
        return "<h2>Services</h2><p>No services are declared.</p>"

    rows = []
    listening_shown = False
    for lease in snapshot.leases:
        health = snapshot.health.get((lease.project, lease.name))
        up = health is not None and health.up
        url = f"http://{lease.host}:{lease.port}/"
        if not snapshot.probed:
            status = "UNKNOWN"
        elif up:
            status = "UP"
        elif _lease_has_listener(snapshot, lease):
            status = "LISTENING"
            listening_shown = True
        else:
            status = "<span class=\"down\">DOWN</span>"
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
    legend = (
        "<p>LISTENING: something holds the leased port but did not answer an "
        "HTTP probe. That is the expected state for a service that does not "
        "speak HTTP, such as an MQTT broker.</p>"
        if listening_shown
        else ""
    )
    return (
        "<h2>Services</h2>"
        + note
        + "<table>"
        "<tr><th>Project</th><th>Address</th><th>State</th><th></th></tr>"
        + "".join(rows)
        + "</table>"
        + legend
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
            """Dispatch one read-only request, and never fail silently.

            The guard is at the dispatch boundary only. A `KeyError` from a
            metrics dict missing a key, or a `ValueError` from anything the
            renderer touches, used to propagate out of the handler with no
            response written at all: the client got an empty reply rather
            than a status, on every request, while the prober went on
            publishing. That contradicts this module's own promise that
            nothing takes the page down. It does not wrap the rendering
            internals, where a blanket catch would hide the bug instead of
            reporting it.
            """
            try:
                self._dispatch()
            except Exception:  # noqa: BLE001 - a request boundary, see above
                self._send(500, "text/plain; charset=utf-8", b"internal error\n")

        def _dispatch(self) -> None:
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", render_page(get_snapshot()))
            elif self.path == "/ports.json":
                snapshot = get_snapshot()
                reasons = _ports_refusals(snapshot)
                if reasons:
                    # Two windows, one refusal. An unprobed snapshot has no
                    # listeners in it -- not because none are found, but
                    # because none were looked for. A snapshot collected
                    # while Docker was unreachable has listeners but cannot
                    # attribute them, and `container` reads as null for every
                    # one: the allocator then sees a project's own container
                    # nowhere on the port it already runs on, and moves it.
                    # Serving either as 200 reads to the allocator as a
                    # verified answer, and `ports/allocate.py` grants on it.
                    # 503 makes `urllib` raise `HTTPError`, which
                    # `ports.live.fetch_live` turns into `LiveUnavailable`,
                    # so the allocator falls back to the refusal it already
                    # has for a page it cannot reach at all. The body says
                    # which window it is, because the operator's next move
                    # differs: wait, or go fix the Docker daemon.
                    body = ("; ".join(reasons) + "\n").encode("utf-8")
                    self._send(503, "text/plain; charset=utf-8", body)
                else:
                    body = json.dumps(ports_payload(snapshot)).encode("utf-8")
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
