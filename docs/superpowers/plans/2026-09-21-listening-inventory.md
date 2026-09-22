# Listening Inventory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show every listening socket on the host — TCP and UDP, with its reachability and what accounts for it — on the status page, and fix the exact-match comparison that hides wildcard binds from the findings.

**Architecture:** A new pure module `inventory.py` classifies each `Listener` by reachability and attributes it to a container, the page's own bind, or a PID. `listening.py` widens to UDP with a `proto` field; the three policy comparisons in `directory.py` gain a TCP guard so datagram sockets cannot satisfy a TCP container's liveness check, and the finding candidate set switches from `==` to `addrs_overlap`. `web.py` renders two tables. Collection stays in collectors, policy stays pure, rendering stays in the renderer.

**Tech Stack:** Python 3.13+, `uv`, `psutil`, stdlib `http.server`, `pytest`. No new runtime dependency.

## Global Constraints

- Run everything with `uv`: `uv run pytest`, never `pip` or `python -m venv`.
- `pyproject.toml` sets `pythonpath = ["src"]`; tests import `harbor_console` with no editable install.
- Collectors never raise on a hostile environment. `listening.py` keeps both degradation paths: a per-connection `except` that skips one malformed socket, and a whole-call `except` that returns `()`.
- Absence of evidence is never a finding, and never an assertion in a table: when Docker cannot be read, the attribution column reads `unknown`, never the unaccounted marker.
- The page is read-only. No new endpoint, no form, no button.
- UDP produces no findings. It appears in the inventory table only.
- Do not chain shell commands with `&&`; run them as separate calls.
- Full spec: `docs/superpowers/specs/2026-09-21-listening-inventory-design.md`.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/harbor_console/listening.py` | collect every listening socket | add `proto`, collect UDP |
| `src/harbor_console/inventory.py` | pure: socket → reachability + attribution | **create** |
| `src/harbor_console/directory.py` | pure: declared-service policy | 3 TCP guards, wildcard fix |
| `src/harbor_console/snapshot.py` | prober→renderer contract | add `inventory` field |
| `src/harbor_console/webapp.py` | coordinate collection | build the inventory |
| `src/harbor_console/web.py` | render | two new tables |
| `tests/test_listening.py` | | helper gains `type`/`raddr`, UDP tests |
| `tests/test_inventory.py` | | **create** |
| `tests/test_directory.py` | | guards, and one inverted test |
| `tests/test_webapp.py` | | inventory reaches the snapshot |
| `tests/test_web.py` | | both tables |
| `docs/adr/0018-show-the-full-listening-inventory.md` | | **create** |
| `CLAUDE.md` | | module list, UDP, wildcard behaviour |

---

### Task 1: Collect UDP in `listening.py`

**Files:**
- Modify: `src/harbor_console/listening.py`
- Test: `tests/test_listening.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Listener(addr: str, port: int, pid: int | None, proto: str = "tcp")`, and module constants `PROTO_TCP = "tcp"`, `PROTO_UDP = "udp"`. `listening_sockets(net_connections=psutil.net_connections) -> tuple[Listener, ...]`, sorted by `(port, addr, proto)`.

Background: psutil's `kind="inet"` returns both stream and datagram sockets. UDP has no `LISTEN` state — an unconnected datagram socket has `status == psutil.CONN_NONE` and an empty `raddr` — so the existing `CONN_LISTEN` filter would silently drop every one of them. Protocol comes from `connection.type`, never from the status.

- [ ] **Step 1: Update the test helper so it can describe both protocols**

The current helper builds a `SimpleNamespace` with no `type` attribute. Reading `connection.type` would raise `AttributeError`, which the per-connection `except` swallows — every existing test would pass for the wrong reason (an empty result). Replace the helper at the top of `tests/test_listening.py`:

```python
import socket
from types import SimpleNamespace

import psutil

from harbor_console.listening import (
    ANY_ADDR,
    PROTO_TCP,
    PROTO_UDP,
    Listener,
    addrs_overlap,
    listening_sockets,
)


def conn(ip, port, status=psutil.CONN_LISTEN, pid=None, sock_type=socket.SOCK_STREAM, raddr=()):
    return SimpleNamespace(
        laddr=SimpleNamespace(ip=ip, port=port),
        status=status,
        pid=pid,
        type=sock_type,
        raddr=raddr,
    )


def udp(ip, port, pid=None, raddr=()):
    return conn(ip, port, status=psutil.CONN_NONE, pid=pid, sock_type=socket.SOCK_DGRAM, raddr=raddr)
```

Also give the one hand-built namespace in `test_a_socket_with_no_local_address_is_skipped` a `type`, so it exercises the missing-address path rather than the missing-attribute path:

```python
def test_a_socket_with_no_local_address_is_skipped():
    conns = [
        SimpleNamespace(laddr=(), status=psutil.CONN_LISTEN, pid=None, type=socket.SOCK_STREAM, raddr=())
    ]

    assert listening_sockets(net_connections=lambda kind: conns) == ()
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_listening.py`:

```python
def test_the_collector_asks_for_both_protocols():
    seen = []

    def net_connections(kind):
        seen.append(kind)
        return []

    listening_sockets(net_connections=net_connections)

    assert seen == ["inet"]


def test_a_bound_udp_socket_is_collected():
    result = listening_sockets(net_connections=lambda kind: [udp("0.0.0.0", 41641, pid=7)])

    assert result == (Listener("0.0.0.0", 41641, 7, PROTO_UDP),)


def test_a_connected_udp_socket_is_not_collected():
    conns = [udp("192.168.86.26", 68, raddr=SimpleNamespace(ip="192.168.86.1", port=67))]

    assert listening_sockets(net_connections=lambda kind: conns) == ()


def test_a_udp_socket_does_not_need_the_listen_state():
    result = listening_sockets(net_connections=lambda kind: [udp("127.0.0.53", 53)])

    assert result[0].proto == PROTO_UDP


def test_tcp_still_requires_the_listen_state():
    conns = [conn("10.0.0.1", 51234, status=psutil.CONN_ESTABLISHED)]

    assert listening_sockets(net_connections=lambda kind: conns) == ()


def test_tcp_defaults_to_the_tcp_proto():
    result = listening_sockets(net_connections=lambda kind: [conn("0.0.0.0", 22)])

    assert result == (Listener("0.0.0.0", 22, None, PROTO_TCP),)


def test_the_same_port_on_both_protocols_is_two_listeners():
    conns = [udp("127.0.0.53", 53), conn("127.0.0.53", 53)]

    result = listening_sockets(net_connections=lambda kind: conns)

    assert [item.proto for item in result] == [PROTO_TCP, PROTO_UDP]


def test_the_ipv6_wildcard_is_normalised_for_udp_too():
    result = listening_sockets(net_connections=lambda kind: [udp("::", 41641)])

    assert result == (Listener("0.0.0.0", 41641, None, PROTO_UDP),)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_listening.py -v`
Expected: FAIL — `ImportError: cannot import name 'PROTO_TCP'` collecting the module.

- [ ] **Step 4: Add the protocol to `Listener`**

In `src/harbor_console/listening.py`, add `import socket` to the imports, then the constants below the existing address constants:

```python
#: The two protocols a listening socket can speak. `proto` defaults to tcp
#: because tcp is all this collector gathered before UDP was added, so every
#: `Listener` built positionally by older code still means what it said.
PROTO_TCP = "tcp"
PROTO_UDP = "udp"
```

and extend the dataclass:

```python
@dataclass(frozen=True)
class Listener:
    """One listening socket. `pid` is None when it belongs to another user."""

    addr: str
    port: int
    pid: int | None
    proto: str = PROTO_TCP
```

- [ ] **Step 5: Collect both protocols**

Replace the body of `listening_sockets` between the `try` block and the `return` with:

```python
    try:
        connections = net_connections(kind="inet")
    except Exception:
        return ()

    found: set[Listener] = set()
    for connection in connections:  # type: ignore[union-attr]
        try:
            proto = PROTO_UDP if connection.type == socket.SOCK_DGRAM else PROTO_TCP
            if proto == PROTO_TCP:
                if connection.status != psutil.CONN_LISTEN:
                    continue
            elif connection.raddr:
                # UDP has no LISTEN state. A datagram socket with a peer is
                # a conversation, not a service waiting to be spoken to.
                continue
            laddr = connection.laddr
            if not laddr:
                continue
            addr = IPV4_ANY if laddr.ip == IPV6_ANY else laddr.ip
            found.add(
                Listener(addr=addr, port=int(laddr.port), pid=connection.pid, proto=proto)
            )
        except (AttributeError, TypeError, ValueError):
            continue

    return tuple(sorted(found, key=lambda item: (item.port, item.addr, item.proto)))
```

Update the docstring's first line to say "Collect listening TCP sockets and bound UDP sockets."

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_listening.py -v`
Expected: PASS, all tests in the file.

- [ ] **Step 7: Run the whole suite**

Run: `uv run pytest`
Expected: PASS. `Listener`'s new field is defaulted, so every positional construction elsewhere still type-checks and still means tcp.

- [ ] **Step 8: Commit**

```bash
git add src/harbor_console/listening.py tests/test_listening.py
git commit -m "feat(listening): collect bound UDP sockets alongside TCP listeners"
```

---

### Task 2: Guard the policy comparisons with the protocol

**Files:**
- Modify: `src/harbor_console/directory.py:170` (`_tcp_row`), `:188` (`_edge_row`), `:287` (the finding candidate set)
- Test: `tests/test_directory.py`

**Interfaces:**
- Consumes: `PROTO_TCP` from `harbor_console.listening` (Task 1).
- Produces: no signature change. `build_rows` and `find_findings` keep their parameters.

Without this, a container publishing TCP 1883 would be reported `LISTENING` because something unrelated holds UDP 1883.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_directory.py`:

```python
def test_a_udp_socket_does_not_make_a_tcp_row_listening():
    mqtt = Container(
        "ice-colder-mqtt",
        (("0.0.0.0", 1883),),
        {"harbor.kind": "tcp", "harbor.port": "1883"},
    )

    rows = build_rows(
        (mqtt,), (), (Listener("0.0.0.0", 1883, None, "udp"),), {}, probed=True
    )

    assert rows[0].state == "DOWN"


def test_a_udp_socket_does_not_make_an_edge_row_listening():
    edge = Container("traefik", ((TAILNET, 443),), {"harbor.kind": "edge"})

    rows = build_rows(
        (edge,), (), (Listener(TAILNET, 443, None, "udp"),), {}, probed=True
    )

    assert rows[0].state == "DOWN"


def test_a_udp_tailnet_listener_is_not_a_finding():
    assert find_findings((), (), (Listener(TAILNET, 41641, None, "udp"),), TAILNET) == ()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_directory.py -k udp -v`
Expected: 3 FAIL — the rows report `LISTENING` and the UDP socket produces an `undeclared-tailnet-listener` finding.

- [ ] **Step 3: Add the guards**

In `src/harbor_console/directory.py`, extend the import:

```python
from harbor_console.listening import PROTO_TCP, Listener, addrs_overlap
```

In `_tcp_row`, replace the `held` expression:

```python
    held = any(
        listener.proto == PROTO_TCP
        and listener.port == port
        and addrs_overlap(listener.addr, addr)
        for addr, _ in published
        for listener in listeners
    )
```

In `_edge_row`:

```python
    held = all(
        any(
            listener.proto == PROTO_TCP
            and listener.port == port
            and addrs_overlap(listener.addr, addr)
            for listener in listeners
        )
        for addr, port in container.published
    ) and bool(container.published)
```

In `find_findings`, the candidate set — protocol only for now, the address comparison is Task 3:

```python
        for addr, port in sorted(
            {(l.addr, l.port) for l in listeners
             if l.proto == PROTO_TCP and l.addr == tailnet_address}
        ):
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_directory.py -v`
Expected: PASS, whole file.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/directory.py tests/test_directory.py
git commit -m "fix(directory): a UDP socket cannot stand in for a TCP service"
```

---

### Task 3: A wildcard bind is reachable on the tailnet

**Files:**
- Modify: `src/harbor_console/directory.py` (`find_findings`, the candidate set and the detail text)
- Test: `tests/test_directory.py:325` — replace `test_a_wildcard_listener_is_not_a_tailnet_finding`

**Interfaces:**
- Consumes: `ANY_ADDR`, `addrs_overlap` from `harbor_console.listening`.
- Produces: no signature change. New detail wording for wildcard binds: `"0.0.0.0:22 is listening on every address including the tailnet, and no container publishes it"`. The tailnet-specific wording is unchanged: `"<addr>:<port> is listening on the tailnet, and no container publishes it"`.

Note for the implementer: `tests/test_directory.py:325` currently **asserts the old behaviour** (`test_a_wildcard_listener_is_not_a_tailnet_finding`). This task deliberately inverts it. Replace that test rather than adding a contradictory one — leaving both would make the suite unsatisfiable.

- [ ] **Step 1: Replace the test that locks in the blind spot**

In `tests/test_directory.py`, delete:

```python
def test_a_wildcard_listener_is_not_a_tailnet_finding():
    assert find_findings((), (), (Listener("0.0.0.0", 22, None),), TAILNET) == ()
```

and put in its place:

```python
def test_a_wildcard_listener_is_a_tailnet_finding():
    findings = find_findings((), (), (Listener("0.0.0.0", 22, None),), TAILNET)

    assert findings == (
        Finding(
            UNDECLARED_TAILNET_LISTENER,
            "0.0.0.0:22 is listening on every address including the tailnet, "
            "and no container publishes it",
        ),
    )


def test_a_wildcard_listener_a_container_publishes_is_not_reported():
    mqtt = Container(
        "ice-colder-mqtt",
        (("0.0.0.0", 1883),),
        {"harbor.kind": "tcp", "harbor.port": "1883"},
    )

    assert find_findings((mqtt,), (), (Listener("0.0.0.0", 1883, None),), TAILNET) == ()


def test_a_loopback_listener_is_not_a_tailnet_finding():
    assert find_findings((), (), (Listener("127.0.0.1", 8081, None),), TAILNET) == ()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_directory.py -k wildcard -v`
Expected: FAIL on `test_a_wildcard_listener_is_a_tailnet_finding` — it returns `()` because the candidate set compares addresses with `==`. The other two pass already; they are there to pin the behaviour that must *not* change.

- [ ] **Step 3: Compare with `addrs_overlap` and word the wildcard case**

In `src/harbor_console/directory.py`, extend the import:

```python
from harbor_console.listening import ANY_ADDR, PROTO_TCP, Listener, addrs_overlap
```

Replace the candidate set and the finding it builds:

```python
        # `addrs_overlap`, not `==`: a process bound to 0.0.0.0 answers on the
        # tailnet address too, and comparing the strings hid every wildcard
        # bind on the host -- sshd included.
        for addr, port in sorted(
            {(l.addr, l.port) for l in listeners
             if l.proto == PROTO_TCP and addrs_overlap(l.addr, tailnet_address)}
        ):
            if port == own_port or EPHEMERAL_MIN <= port <= EPHEMERAL_MAX:
                continue
            if any(p == port and addrs_overlap(a, addr) for a, p in published):
                continue
            where = (
                "every address including the tailnet" if addr == ANY_ADDR else "the tailnet"
            )
            findings.append(
                Finding(
                    UNDECLARED_TAILNET_LISTENER,
                    f"{addr}:{port} is listening on {where}, and no container publishes it",
                )
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_directory.py -v`
Expected: PASS, whole file — including `test_findings_come_in_a_stable_order`, whose listener is on the tailnet address and whose container publishes a port nothing listens on, so its finding set is unchanged.

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_console/directory.py tests/test_directory.py
git commit -m "fix(directory): a wildcard bind answers on the tailnet, so report it"
```

---

### Task 4: `inventory.py` — reachability and attribution

**Files:**
- Create: `src/harbor_console/inventory.py`
- Test: `tests/test_inventory.py`

**Interfaces:**
- Consumes: `Listener`, `ANY_ADDR`, `IPV6_ANY`, `addrs_overlap` from `harbor_console.listening`; `Container`, `DOCKER_UNAVAILABLE` from `harbor_console.docker`.
- Produces:
  - `Entry(proto: str, addr: str, port: int, reach: str, accounted: str)`, frozen.
  - `REACH_LOOPBACK = "loopback"`, `REACH_TAILNET = "tailnet"`, `REACH_LAN = "LAN"`, `REACH_ANY = "LAN + tailnet"`, `UNKNOWN = "unknown"`, `OWN_NAME = "harbor-console-web"`.
  - `reach_of(addr: str, tailnet_address: str | None) -> str`
  - `build_inventory(listeners: Sequence[Listener], containers: Sequence[Container], tailnet_address: str | None, own_port: int | None = None) -> tuple[Entry, ...]`, sorted by `(port, addr, proto)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_inventory.py`:

```python
from harbor_console.docker import DOCKER_UNAVAILABLE, Container
from harbor_console.inventory import (
    OWN_NAME,
    REACH_ANY,
    REACH_LAN,
    REACH_LOOPBACK,
    REACH_TAILNET,
    UNKNOWN,
    Entry,
    build_inventory,
    reach_of,
)
from harbor_console.listening import Listener

TAILNET = "100.69.239.123"
TAILNET_V6 = "fd7a:115c:a1e0::7b37:ef7d"


def test_the_wildcard_reaches_the_lan_and_the_tailnet():
    assert reach_of("0.0.0.0", TAILNET) == REACH_ANY


def test_the_ipv6_wildcard_reaches_the_lan_and_the_tailnet():
    assert reach_of("::", TAILNET) == REACH_ANY


def test_the_tailnet_address_reaches_the_tailnet():
    assert reach_of(TAILNET, TAILNET) == REACH_TAILNET


def test_a_tailscale_ula_address_reaches_the_tailnet():
    assert reach_of(TAILNET_V6, TAILNET) == REACH_TAILNET


def test_loopback_reaches_only_itself():
    assert reach_of("127.0.0.1", TAILNET) == REACH_LOOPBACK
    assert reach_of("127.0.0.53", TAILNET) == REACH_LOOPBACK
    assert reach_of("::1", TAILNET) == REACH_LOOPBACK


def test_another_address_reaches_the_lan():
    assert reach_of("192.168.86.26", TAILNET) == REACH_LAN


def test_a_link_local_address_reaches_the_lan():
    assert reach_of("fe80::a28c:fdff:fee8:3e59", TAILNET) == REACH_LAN


def test_an_unparseable_address_reaches_the_lan():
    assert reach_of("fe80::1%eno1", TAILNET) == REACH_LAN


def test_reachability_without_a_tailnet_address_still_classifies():
    assert reach_of("127.0.0.1", None) == REACH_LOOPBACK
    assert reach_of("192.168.86.26", None) == REACH_LAN


def test_a_listener_is_attributed_to_the_container_that_publishes_it():
    mqtt = Container("ice-colder-mqtt", (("0.0.0.0", 1883),), {"harbor.kind": "tcp"})

    result = build_inventory((Listener("0.0.0.0", 1883, None),), (mqtt,), TAILNET, 8100)

    assert result == (Entry("tcp", "0.0.0.0", 1883, REACH_ANY, "ice-colder-mqtt"),)


def test_attribution_overlaps_addresses_rather_than_matching_them():
    traefik = Container("traefik", ((TAILNET, 443),), {"harbor.kind": "edge"})

    result = build_inventory((Listener("0.0.0.0", 443, None),), (traefik,), TAILNET, 8100)

    assert result[0].accounted == "traefik"


def test_the_pages_own_bind_is_attributed_to_the_page():
    result = build_inventory((Listener(TAILNET, 8100, None),), (), TAILNET, 8100)

    assert result[0].accounted == OWN_NAME


def test_a_readable_pid_is_the_fallback_attribution():
    result = build_inventory((Listener("0.0.0.0", 22, 812),), (), TAILNET, 8100)

    assert result[0].accounted == "pid 812"


def test_a_listener_nothing_accounts_for_is_left_empty():
    result = build_inventory((Listener("0.0.0.0", 22, None),), (), TAILNET, 8100)

    assert result[0].accounted == ""


def test_attribution_is_unknown_when_docker_could_not_be_read():
    result = build_inventory(
        (Listener("0.0.0.0", 1883, None),), DOCKER_UNAVAILABLE, TAILNET, 8100
    )

    assert result[0].accounted == UNKNOWN


def test_a_readable_pid_still_beats_unknown_without_docker():
    result = build_inventory(
        (Listener("0.0.0.0", 22, 812),), DOCKER_UNAVAILABLE, TAILNET, 8100
    )

    assert result[0].accounted == "pid 812"


def test_the_pages_own_bind_is_known_without_docker():
    result = build_inventory(
        (Listener(TAILNET, 8100, None),), DOCKER_UNAVAILABLE, TAILNET, 8100
    )

    assert result[0].accounted == OWN_NAME


def test_udp_carries_its_protocol_through():
    result = build_inventory((Listener("0.0.0.0", 41641, None, "udp"),), (), TAILNET, 8100)

    assert result[0].proto == "udp"
    assert result[0].reach == REACH_ANY


def test_entries_are_ordered_by_port_then_address_then_protocol():
    listeners = (
        Listener("127.0.0.1", 8081, None),
        Listener("0.0.0.0", 53, None, "udp"),
        Listener("0.0.0.0", 53, None),
        Listener("0.0.0.0", 22, None),
    )

    result = build_inventory(listeners, (), TAILNET, 8100)

    assert [(e.port, e.proto) for e in result] == [
        (22, "tcp"),
        (53, "tcp"),
        (53, "udp"),
        (8081, "tcp"),
    ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_inventory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor_console.inventory'`.

- [ ] **Step 3: Write the module**

Create `src/harbor_console/inventory.py`:

```python
"""Every listening socket, classified by who can reach it and what accounts for it.

Pure: listeners, containers and the host's tailnet address in; one entry per
socket out. The page's four findings answer "where do the declarations and the
host disagree"; this answers the prior question, "what is listening at all",
which is the one a rule cannot be trusted with -- a filter applied before
anyone can look is unfalsifiable by inspection.

Two deliberate limitations:

- **Attribution ignores protocol.** `docker.py` drops the protocol from
  `NetworkSettings.Ports`, so `Container.published` is `(addr, port)` only. A
  UDP listener therefore attributes to a container publishing the same TCP
  port. The entry still reports the true protocol, and threading protocol
  through `published` would ripple into `directory.py` for a case that does
  not occur on this host.
- **Most PIDs are unreadable.** The service runs unprivileged, and psutil
  cannot map socket inode to PID for another user's socket, so sshd,
  tailscaled and dockerd arrive with `pid=None`. The inventory is complete;
  the attribution is partial, and an unattributed row is the signal rather
  than a defect.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from ipaddress import ip_address

from harbor_console.docker import DOCKER_UNAVAILABLE, Container
from harbor_console.listening import ANY_ADDR, IPV6_ANY, Listener, addrs_overlap

#: Who can reach a socket, given the address it is bound to.
REACH_LOOPBACK = "loopback"
REACH_TAILNET = "tailnet"
REACH_LAN = "LAN"
REACH_ANY = "LAN + tailnet"

#: Tailscale's fixed ULA allocation. `tailnet.py` runs `tailscale ip -4` and
#: never learns this host's IPv6 tailnet address, so without matching the
#: prefix the tailnet's own IPv6 listeners would be labelled LAN. The prefix
#: is a documented constant of Tailscale's, not a fact about this host.
TAILNET_ULA_PREFIX = "fd7a:115c:a1e0:"

#: Attribution when Docker could not be read: the truth is that we do not
#: know, which is not the same as nothing accounting for the socket.
UNKNOWN = "unknown"

#: Attribution for the page's own bind -- a host process no container
#: publishes, and the one such listener that is declared by being this program.
OWN_NAME = "harbor-console-web"


@dataclass(frozen=True)
class Entry:
    """One listening socket, with who can reach it and what accounts for it."""

    proto: str
    addr: str
    port: int
    reach: str
    #: A container name, `OWN_NAME`, "pid <n>", `UNKNOWN`, or "" for a socket
    #: nothing accounts for.
    accounted: str


def reach_of(addr: str, tailnet_address: str | None) -> str:
    """Who can reach a socket bound to `addr`.

    Link-local `fe80::/10` counts as LAN: a DHCPv6 client socket is reachable
    from the link, which is the LAN, and a fifth class for it would be
    precision nobody reads. An address that will not parse -- one carrying a
    scope suffix, say -- lands there too.
    """
    if addr in (ANY_ADDR, IPV6_ANY):
        return REACH_ANY
    if tailnet_address is not None and addr == tailnet_address:
        return REACH_TAILNET
    if addr.lower().startswith(TAILNET_ULA_PREFIX):
        return REACH_TAILNET
    try:
        if ip_address(addr).is_loopback:
            return REACH_LOOPBACK
    except ValueError:
        return REACH_LAN
    return REACH_LAN


def _accounted(
    listener: Listener,
    containers: Sequence[Container],
    tailnet_address: str | None,
    own_port: int | None,
    docker_available: bool,
) -> str:
    """What accounts for this socket, most informative answer first."""
    if (
        own_port is not None
        and listener.port == own_port
        and tailnet_address is not None
        and listener.addr == tailnet_address
    ):
        return OWN_NAME
    if docker_available:
        for container in sorted(containers, key=lambda item: item.name):
            for addr, port in container.published:
                if port == listener.port and addrs_overlap(addr, listener.addr):
                    return container.name
    if listener.pid is not None:
        return f"pid {listener.pid}"
    return "" if docker_available else UNKNOWN


def build_inventory(
    listeners: Sequence[Listener],
    containers: Sequence[Container],
    tailnet_address: str | None,
    own_port: int | None = None,
) -> tuple[Entry, ...]:
    """One entry per listening socket, ordered as the collector orders them."""
    docker_available = containers is not DOCKER_UNAVAILABLE
    entries = [
        Entry(
            proto=listener.proto,
            addr=listener.addr,
            port=listener.port,
            reach=reach_of(listener.addr, tailnet_address),
            accounted=_accounted(
                listener, containers, tailnet_address, own_port, docker_available
            ),
        )
        for listener in listeners
    ]
    return tuple(sorted(entries, key=lambda item: (item.port, item.addr, item.proto)))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_inventory.py -v`
Expected: PASS, all tests.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/inventory.py tests/test_inventory.py
git commit -m "feat(inventory): classify every listening socket by reach and owner"
```

---

### Task 5: Carry the inventory to the renderer

**Files:**
- Modify: `src/harbor_console/snapshot.py`, `src/harbor_console/webapp.py`
- Test: `tests/test_webapp.py`

**Interfaces:**
- Consumes: `build_inventory` and `Entry` from `harbor_console.inventory` (Task 4).
- Produces: `Snapshot.inventory: tuple[Entry, ...] = ()`, populated by `collect_snapshot` from the same `listeners()` and `containers()` results the rows and findings use.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_webapp.py`:

```python
def test_collect_snapshot_builds_the_listening_inventory():
    snapshot = collect(listeners=lambda: (Listener("0.0.0.0", 22, None),))

    assert [(e.addr, e.port, e.reach, e.accounted) for e in snapshot.inventory] == [
        ("0.0.0.0", 22, "LAN + tailnet", "")
    ]


def test_the_inventory_knows_the_pages_own_bind():
    snapshot = collect(listeners=lambda: (Listener("100.69.239.123", 8100, None),))

    assert snapshot.inventory[0].accounted == "harbor-console-web"


def test_the_inventory_is_unknown_when_docker_is_unavailable():
    snapshot = collect(
        listeners=lambda: (Listener("0.0.0.0", 1883, None),),
        containers=lambda: DOCKER_UNAVAILABLE,
    )

    assert snapshot.inventory[0].accounted == "unknown"
```

Add to that file's imports whatever it is missing — `from harbor_console.listening import Listener` and `DOCKER_UNAVAILABLE` from `harbor_console.docker` (check the existing import block first; `DOCKER_UNAVAILABLE` is likely already there for the existing unavailability tests).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_webapp.py -k inventory -v`
Expected: FAIL — `AttributeError: 'Snapshot' object has no attribute 'inventory'`.

- [ ] **Step 3: Add the field to the snapshot**

In `src/harbor_console/snapshot.py`, add the import:

```python
from harbor_console.inventory import Entry
```

and the field, next to `listeners`:

```python
    listeners: tuple[Listener, ...] = ()
    #: One entry per listening socket: what it is, who can reach it, and what
    #: accounts for it. Derived from `listeners` and `containers` so the
    #: renderer does no policy of its own.
    inventory: tuple[Entry, ...] = ()
```

- [ ] **Step 4: Build it in the prober**

In `src/harbor_console/webapp.py`, add to the imports:

```python
from harbor_console.inventory import build_inventory
```

and to the `Snapshot(...)` construction in `collect_snapshot`, directly after the `listeners=found,` line:

```python
        inventory=build_inventory(found, running, tailnet_address, own_port),
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_webapp.py -v`
Expected: PASS, whole file.

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/harbor_console/snapshot.py src/harbor_console/webapp.py tests/test_webapp.py
git commit -m "feat(webapp): carry the listening inventory in the snapshot"
```

---

### Task 6: Render the inventory as two tables

**Files:**
- Modify: `src/harbor_console/web.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `Snapshot.inventory` (Task 5), `Entry` and `REACH_LOOPBACK` from `harbor_console.inventory`.
- Produces: `_inventory_section(snapshot: Snapshot) -> str`, called from `render_page` after `_findings_section`. No new endpoint.

- [ ] **Step 1: Write the failing tests**

Add to the imports at the top of `tests/test_web.py`:

```python
from harbor_console.inventory import REACH_ANY, REACH_LOOPBACK, REACH_TAILNET, Entry
```

and append the tests:

```python
SSHD = Entry("tcp", "0.0.0.0", 22, REACH_ANY, "")
MQTT_SOCKET = Entry("tcp", "0.0.0.0", 1883, REACH_ANY, "ice-colder-mqtt")
WIREGUARD = Entry("udp", "0.0.0.0", 41641, REACH_ANY, "")
API = Entry("tcp", "127.0.0.1", 8081, REACH_LOOPBACK, "traefik")
TAILNET_V6_SOCKET = Entry("tcp", "fd7a:115c:a1e0::7b37:ef7d", 51365, REACH_TAILNET, "")


def test_page_lists_what_is_listening():
    page = web.render_page(snapshot(inventory=(SSHD, MQTT_SOCKET, WIREGUARD))).decode()

    assert "Listening" in page
    assert "0.0.0.0:22" in page
    assert "0.0.0.0:1883" in page
    assert "ice-colder-mqtt" in page
    assert escape(REACH_ANY) in page


def test_page_shows_the_protocol_of_each_socket():
    page = web.render_page(snapshot(inventory=(WIREGUARD,))).decode()

    assert "<td>udp</td>" in page


def test_loopback_sockets_go_in_their_own_table():
    page = web.render_page(snapshot(inventory=(SSHD, API))).decode()

    listening, loopback = page.split("Loopback only")
    assert "0.0.0.0:22" in listening
    assert "127.0.0.1:8081" not in listening
    assert "127.0.0.1:8081" in loopback


def test_a_socket_nothing_accounts_for_is_marked():
    page = web.render_page(snapshot(inventory=(SSHD,))).decode()

    assert 'class="unaccounted"' in page


def test_an_accounted_socket_is_not_marked():
    page = web.render_page(snapshot(inventory=(MQTT_SOCKET,))).decode()

    assert 'class="unaccounted"' not in page


def test_an_ipv6_address_is_bracketed():
    page = web.render_page(snapshot(inventory=(TAILNET_V6_SOCKET,))).decode()

    assert "[fd7a:115c:a1e0::7b37:ef7d]:51365" in page


def test_the_loopback_table_is_omitted_when_nothing_is_on_loopback():
    page = web.render_page(snapshot(inventory=(SSHD,))).decode()

    assert "Loopback only" not in page


def test_nothing_listening_on_a_reachable_address_says_so():
    page = web.render_page(snapshot(inventory=(API,))).decode()

    assert "Nothing is listening on a reachable address" in page


def test_the_inventory_is_unknown_before_the_first_cycle():
    page = web.render_page(snapshot(probed=False, inventory=())).decode()

    assert "so what is listening is unknown" in page


def test_an_unknown_attribution_is_not_marked_unaccounted():
    unknown = Entry("tcp", "0.0.0.0", 1883, REACH_ANY, "unknown")

    page = web.render_page(snapshot(inventory=(unknown,), docker_available=False)).decode()

    assert "unknown" in page
    assert 'class="unaccounted"' not in page
```

Two existing tests in this file need adjusting, both because the new section is real rather than because it is wrong:

`test_page_does_not_call_an_unprobed_host_clean` counts the "nothing has been collected yet" copy, and there are now three such sections rather than two:

```python
    assert page.lower().count("nothing has been collected yet") == 3
```

`test_page_escapes_every_field_that_originates_outside_this_project` must cover the new column: a container name comes from Docker, which is outside this project. Extend it:

```python
def test_page_escapes_every_field_that_originates_outside_this_project():
    evil = Row("<b>n</b>", KIND_HTTP, "https://x/?a=<s>", "<i>c</i>", "<u>d</u>", "UP")
    finding = Finding("<k>", "<d>")
    socket = Entry("<p>", "<a>", 1, REACH_ANY, "<c>")
    metrics = dict(METRICS, hostname="<h>")

    page = web.render_page(
        snapshot(rows=(evil,), findings=(finding,), inventory=(socket,), metrics=metrics)
    ).decode()

    for raw in ("<b>n</b>", "<s>", "<i>c</i>", "<u>d</u>", "<k>", "<d>", "<h>", "<p>", "<a>", "<c>"):
        assert raw not in page
        assert escape(raw) in page
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web.py -k "listening or loopback or unaccounted or bracketed or inventory" -v`
Expected: FAIL — the page has no such section.

- [ ] **Step 3: Add the CSS class**

In `src/harbor_console/web.py`, add one line inside `_STYLE`, after the `.down` rule:

```css
.unaccounted { font-weight: 700; }
```

- [ ] **Step 4: Write the section**

Add the import:

```python
from harbor_console.inventory import REACH_LOOPBACK, Entry
```

and the functions, below `_findings_section`:

```python
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
```

- [ ] **Step 5: Call it**

In `render_page`, after the `_findings_section` append:

```python
    parts.append(_inventory_section(snapshot))
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web.py -v`
Expected: PASS, whole file.

- [ ] **Step 7: Run the whole suite**

Run: `uv run pytest`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/harbor_console/web.py tests/test_web.py
git commit -m "feat(web): render the listening inventory, loopback split out"
```

---

### Task 7: Record the decision

**Files:**
- Create: `docs/adr/0018-show-the-full-listening-inventory.md`
- Modify: `CLAUDE.md`, `docs/adr/README.md`

**Interfaces:**
- Consumes: the behaviour built in Tasks 1–6.
- Produces: documentation only. No code.

- [ ] **Step 1: Write ADR 18**

Create `docs/adr/0018-show-the-full-listening-inventory.md`, following `docs/adr/template.md`:

```markdown
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
```

- [ ] **Step 2: Add it to the ADR index**

In `docs/adr/README.md`, append one row to the Records table, directly below the `0017` row:

```markdown
| 0018 | [Show the full listening inventory, not only what a rule anticipates](0018-show-the-full-listening-inventory.md) | Accepted |
```

- [ ] **Step 3: Update `CLAUDE.md`**

Three edits, by line.

`CLAUDE.md:40` — the module count:

```markdown
The web surface is ten modules at the top level, and keeps the same split:
```

`CLAUDE.md:43` — replace the `listening.py` bullet:

```markdown
- `listening.py` — **collects** every listening TCP socket and every bound UDP socket via `psutil`, including loopback-bound and non-Docker ones. UDP has no `LISTEN` state, so a datagram socket counts when it has no peer; `Listener.proto` tells the two apart, and the policy in `directory.py` requires `tcp` wherever it checks a TCP service's liveness. IPv6 `::` is normalised to `0.0.0.0`, and `addrs_overlap` lives here: the wildcard contends with every address on its host, two specific addresses do not contend.
```

`CLAUDE.md:47` — after the `directory.py` bullet, insert a new one:

```markdown
- `inventory.py` — the other policy, also pure: listeners and containers in, one entry per socket out, carrying who can reach it (`loopback`, `tailnet`, `LAN`, `LAN + tailnet`) and what accounts for it (a container, this page, a PID, or nothing). Where `directory.py` reports the declared services, this reports what is listening at all — the question a finding rule cannot be trusted with, because a filter applied before anyone can look is unfalsifiable by inspection, which is how a wildcard bind stayed hidden from `undeclared-tailnet-listener` ([ADR 18](docs/adr/0018-show-the-full-listening-inventory.md)). UDP appears here and generates no findings.
```

Then in that same `directory.py` bullet, after "Absence of evidence is never a finding.", add one sentence:

```markdown
`undeclared-tailnet-listener` counts a wildcard bind as reaching the tailnet, because `0.0.0.0` answers there too.
```

- [ ] **Step 4: Run the whole suite one more time**

Run: `uv run pytest`
Expected: PASS. Docs-only task, so this is a guard against an accidental edit.

- [ ] **Step 5: Commit**

```bash
git add docs/adr/0018-show-the-full-listening-inventory.md docs/adr/README.md CLAUDE.md
git commit -m "docs: ADR 18, the page shows the full listening inventory"
```

---

## Verification

After Task 7, check the real thing rather than only the tests. The dev machine is the gateway to the Docker host, not the host itself, so run the page on `hpz440`:

- [ ] `uv run pytest` — whole suite green.
- [ ] `ssh gte@hpz440 "ss -tuln"` — keep the output; it is the oracle for the next step.
- [ ] Deploy per `MEMORY.md` (pull `/srv/harbor-console`, `sudo bash install.sh` as `conrad`, who can sudo — `gte` cannot), then read `https://harbor.hpz440.ohr3023.org/`.
- [ ] Every socket in the `ss` output appears in one of the two tables, and nothing appears that `ss` does not show. Expect `0.0.0.0:41641` (udp, tailscaled) and `0.0.0.0:22` (tcp, sshd) among the unaccounted reachable rows, `0.0.0.0:1883` attributed to `ice-colder-mqtt`, and `127.0.0.1:8081` attributed to `traefik` in the loopback table.
- [ ] Findings show exactly one new entry, `0.0.0.0:22`.
