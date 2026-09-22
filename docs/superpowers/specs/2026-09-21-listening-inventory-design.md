# Listening inventory: show every port, not only the ones a rule anticipates

Date: 2026-09-21
Status: approved, not yet implemented

## The problem

The page reports four findings. Everything else its collectors gather is
discarded before it reaches a reader. `Snapshot.listeners` carries every
listening socket on the host and `web.py` renders none of it, so the only path
from a socket to a human eye is a finding rule firing.

That makes the page blind in exactly the way its owner cannot check. Three
concrete gaps, found on 2026-09-21 against the live host:

1. **Wildcard binds never become findings.** `find_findings` selects its
   candidates with `l.addr == tailnet_address` -- an exact string match
   (`directory.py:287`). A host process bound to `0.0.0.0` answers on the
   tailnet address and is silently skipped. `addrs_overlap` exists for this
   comparison and is applied only to the `published` side, one line below.
   Today `0.0.0.0:22` (sshd) is reachable on the tailnet and reported nowhere.

2. **Nothing renders the inventory.** Anything the four rules do not match is
   collected and dropped, including the whole ephemeral range that
   `directory.py:212` deliberately skips.

3. **UDP is never collected.** `listening_sockets` passes `kind="tcp"`, so
   tailscaled's WireGuard port, any resolver and any mDNS responder are
   outside the inventory entirely.

A port scan of 1024-65535 was considered and rejected: it sees strictly less
than the kernel's own socket table (no PID, no loopback-only listener, no UDP),
adds SYN noise and a scan/render race, and does nothing about the fact that
there is nowhere on the page to show the result.

## Ground truth at design time

TCP (`ss -tln`):

```
0.0.0.0:22                                sshd
0.0.0.0:1883, [::]:1883                   ice-colder-mqtt (harbor.kind=tcp, harbor.port=1883)
100.69.239.123:80, :443                   traefik
100.69.239.123:8100                       harbor-console-web
100.69.239.123:53678                      tailscaled peerapi (ephemeral)
127.0.0.1:8081                            traefik API
127.0.0.53:53, 127.0.0.54:53              systemd-resolved
[fd7a:115c:a1e0::7b37:ef7d]:51365         tailscaled
```

UDP (`ss -uln`), none of which the page can currently see:

```
0.0.0.0:41641, [::]:41641                 tailscaled WireGuard
127.0.0.53:53, 127.0.0.54:53              systemd-resolved
192.168.86.26:68                          DHCP client
[fe80::a28c:fdff:fee8:3e59]:546           DHCPv6 client
```

(`ss` prints a `%lo` / `%eno1` scope suffix on some of these; that is its
formatting, not part of the address psutil returns.)

Three facts this establishes. The overlap fix yields exactly one new finding
(sshd), not a flood. `ice-colder-mqtt` is bound to `0.0.0.0`, so MQTT answers
on the whole LAN while every other service is tailnet-only -- correctly
declared, so no rule fires, and the page has never shown it. And tailscaled's
WireGuard port is a wildcard UDP bind, so the single largest thing the page
cannot see today is also LAN-reachable.

## What gets built

### `inventory.py` -- a new pure module

`directory.py` answers "are the declared services healthy". This answers "what
is listening at all": a different question over the same inputs, kept out of a
module already near 300 lines.

```python
REACH_LOOPBACK = "loopback"       # 127.0.0.0/8, ::1
REACH_TAILNET  = "tailnet"        # the host's tailnet address
REACH_LAN      = "LAN"            # some other specific address
REACH_ANY      = "LAN + tailnet"  # 0.0.0.0 or ::

@dataclass(frozen=True)
class Entry:
    proto: str      # "tcp" | "udp"
    addr: str
    port: int
    reach: str      # one of the four constants above
    accounted: str  # container name, "harbor-console-web", "pid 1234", "unknown", or ""

def build_inventory(
    listeners: Sequence[Listener],
    containers: Sequence[Container],
    tailnet_address: str | None,
    own_port: int | None,
) -> tuple[Entry, ...]: ...
```

Ordering is `(port, addr, proto)`, matching the collector.

**Reachability.** Loopback is `127.0.0.0/8` or `::1`. The wildcards `0.0.0.0`
and `::` are `REACH_ANY`. The host's tailnet address is `REACH_TAILNET`, and so
is any address inside `fd7a:115c:a1e0::/48`: `tailnet.py` runs
`tailscale ip -4` and never learns the host's IPv6 tailnet address, so without
this the tailnet's own IPv6 listener would be mislabelled `LAN`. That prefix is
Tailscale's fixed documented allocation; it lives in a named constant with a
comment saying why it is hardcoded. Everything else is `REACH_LAN`, link-local
`fe80::/10` included: a DHCPv6 client socket is reachable from the link, which
is the LAN, and a fifth class for it would be precision nobody reads.

Note that `listening.py` already normalises `::` to `0.0.0.0`, so a dual-stack
wildcard bind collapses to one entry rather than two. That is why `[::]:1883`
and `[::]:41641` do not appear separately in the rendering below.

**Attribution**, in order: a container whose published `(addr, port)` matches
the port and whose address overlaps (`addrs_overlap`) gives its name; the
page's own bind (`own_port` on the tailnet address) gives
`"harbor-console-web"`; a readable `Listener.pid` gives `"pid <n>"`; otherwise
the empty string, which the renderer shows as unaccounted.

Two deliberate limitations, both documented in the module docstring:

- **Attribution ignores protocol.** `docker.py:130` drops the proto from
  `NetworkSettings.Ports`, so `Container.published` is `(addr, port)` only. A
  UDP listener attributes to a container publishing the same TCP port. Adding
  proto to `published` would ripple into `_tcp_row` and `_edge_row` for a case
  that does not occur on this host; the row still shows the true proto.
- **Most PIDs are unreadable.** The service runs as `harbor` with
  `CapabilityBoundingSet=` empty. Linux still lists every socket
  (`/proc/net/tcp` is world-readable), but psutil cannot map inode to PID for
  another user's socket, so sshd, tailscaled and dockerd report `pid=None`. The
  inventory is complete; the attribution is partial. Raising privilege to fill
  the column in was rejected -- it would spend the hardening of ADR 16 on a
  label, and an unattributed row is the signal, not a defect.

### `listening.py` -- collect UDP

```python
@dataclass(frozen=True)
class Listener:
    addr: str
    port: int
    pid: int | None
    proto: str = "tcp"   # defaulted: tcp is all this ever collected before
```

`net_connections(kind="inet")` replaces `kind="tcp"`. Two shapes are kept: TCP
with `status == CONN_LISTEN`, and UDP (`type == socket.SOCK_DGRAM`) bound with
no peer -- UDP has no `LISTEN` state, which is why the existing `CONN_LISTEN`
filter would drop every datagram socket. Proto comes from `connection.type`,
never from the status. Both degradation paths are unchanged: a per-connection
`except` skips one malformed entry, and a failing call returns `()`.

### `directory.py` -- three proto guards and the bug fix

The two `held` checks (`_tcp_row:170`, `_edge_row:188`) and the finding
candidate set each gain `listener.proto == "tcp"`, so a datagram socket cannot
satisfy a TCP container's liveness check. The candidate set is also the fix:

```python
for addr, port in sorted({(l.addr, l.port) for l in listeners
                          if l.proto == "tcp" and addrs_overlap(l.addr, tailnet_address)}):
```

The container-publishes check below it already uses `addrs_overlap`, so
`0.0.0.0:1883` still resolves to `ice-colder-mqtt` and stays silent. The
finding's detail text gains a wildcard wording -- "`0.0.0.0:22` is listening on
every address including the tailnet, and no container publishes it" -- because
the current sentence reads wrong for a bind that is not on the tailnet
specifically.

Nothing else in the finding set changes. UDP produces no findings: tailscaled
rebinds its port on every restart, which is the churn the ephemeral filter
already exists to suppress, and a finding that fires on every reboot is one
you learn to ignore. The ephemeral range and the `own_port` skip both stand.

### `snapshot.py` and `webapp.py`

`Snapshot` gains `inventory: tuple[Entry, ...] = ()`, built in
`collect_snapshot` beside `rows` and `findings`. The renderer stays a renderer.

### `web.py` -- two tables

`_inventory_section(snapshot)` renders last: findings remain the alert, this is
the reference. Columns `Proto | Address:Port | Reach | Accounted for`, split
into a `Listening` table (`reach != REACH_LOOPBACK`) and a `Loopback only`
table.

Rendered against the ground truth above:

```
Listening
  tcp  0.0.0.0:22                        LAN + tailnet   -
  udp  192.168.86.26:68                  LAN             -
  tcp  0.0.0.0:1883                      LAN + tailnet   ice-colder-mqtt
  tcp  100.69.239.123:80                 tailnet         traefik
  tcp  100.69.239.123:8100               tailnet         harbor-console-web
  udp  0.0.0.0:41641                     LAN + tailnet   -
  tcp  100.69.239.123:53678              tailnet         -
  tcp  [fd7a:115c:a1e0::7b37:ef7d]:51365 tailnet         -
  udp  [fe80::a28c:fdff:fee8:3e59]:546   LAN             -
Loopback only
  tcp  127.0.0.1:8081                    loopback        traefik
  udp  127.0.0.53:53                     loopback        -
```

Unaccounted rows get their own CSS class so the eye lands on them, as `.down`
already does for a dead route.

**Degradation.** When Docker cannot be read, the column shows `unknown`, never
the unaccounted marker: asserting "nothing accounts for this" from missing
evidence is the table's version of the mistake the finding rules take care not
to make. The existing Docker banner already says why. When `probed` is false,
the section carries the same "nothing has been collected yet" message as the
other two.

## Why not a `lan-exposed` finding

Considered and deferred. It would name the real discovery -- MQTT on the whole
LAN -- but a finding is defined here as a disagreement between a declaration
and the host, and `harbor.kind=tcp` says nothing about bind address, so MQTT
violates nothing it declared. Reachability is a column instead. If
`LAN + tailnet` on a service turns out to be unacceptable, that is a
deliberate follow-up with its own ADR, taken after the page has been showing
it for a while.

## Testing

TDD, tests first, no real sockets and no real Docker.

- `test_listening.py` -- UDP collected; UDP with a peer excluded; TCP still
  `LISTEN`-only; proto in the sort key; both degradation paths unchanged.
- `test_inventory.py` (new) -- all four reach classes, including `::`, an
  `fd7a:` address and an `fe80:` one; attribution via `addrs_overlap`; `own_port` gives
  `harbor-console-web`; pid fallback; unaccounted; Docker unavailable gives
  `unknown`.
- `test_directory.py` -- a wildcard listener now yields the finding; a wildcard
  the container publishes still does not; a UDP socket cannot make a TCP row
  `LISTENING`; UDP yields no findings.
- `test_web.py` -- both tables render; loopback is split out; unaccounted is
  marked; the not-probed and Docker-unavailable cases.

## Record

ADR 18: the page shows the full listening inventory rather than only what a
finding rule anticipates. That is a change in what the page is for, and it is
the reason the wildcard blind spot went unseen -- a rule that filters before
anyone can look is unfalsifiable by inspection.

No new runtime dependency: psutil is already used, and stdlib `http.server`
still serves one page.
