# Storage reporting: one percentage for one filesystem, on a host with nine

Date: 2026-09-23
Status: approved, not yet implemented

## The problem

Both surfaces report storage as one number, from one filesystem:

```python
"disk_utilization": psutil.disk_usage("/").percent,   # system.py
```

```
Disk utilization      70.0%
```

That is the root filesystem and nothing else. It is the same defect the
memory row had before the 2026-09-22 design -- a percentage with no scale --
and one more on top: the scale it omits is not the only thing it omits. On
hpz440 the number above describes a 98 GiB volume while the host carries
2.7 TiB.

Ground truth at design time, from `df -h` and `lsblk -b` on hpz440:

| What | Size | Shown today |
|---|---|---|
| `/` (`ubuntu--vg-ubuntu--lv`, ext4) | 98 GiB, 70% used | yes, as `70.0%` |
| `/home/arm/media` (`ubuntu--vg-media`, ext4) | 2.4 TiB | **no** |
| `/boot` (ext4) | 2 GiB, 17% used | no |
| `/boot/efi` (vfat) | 1.1 GiB, 1% used | no |
| Unallocated space in `ubuntu-vg` | 233.9 GiB | **no, and `df` cannot see it** |
| `/mnt/nas/organized`, `/mnt/nas/photos` (CIFS) | 11 TiB each, one NAS | no |
| 8 snap mounts (squashfs, read-only) | ~600 MiB total | no |
| `sr0`, `sr1`, `sr2` (optical) | 2 holding discs | no |

The console's job is the at-a-glance verdict on this machine. It currently
answers that question about 3.5% of its storage. A full `/home/arm/media`
would be invisible, and so would the 233.9 GiB of volume-group slack that
decides whether that filesystem can grow.

## What this is not

Scope was settled before design, and these are deliberately out:

- **LVM tooling.** `vgs` and `lvs` need root; the console runs as the
  unprivileged `harbor` user (ADR 5) and gets `Permission denied` on the LVM
  lock. Volume-group slack is instead derived from `lsblk`, which works
  unprivileged, as PV size minus the sum of its logical volumes. The number
  is approximate by a few MiB of metadata and honest about being derived.
- **Measuring the NAS.** `statvfs` on an unreachable CIFS mount blocks, and
  blocking is fatal to a 1 Hz refresh loop and to the web prober. The mounts
  are enumerated (safe) and never measured.
- **Optical drives, and multi-host aggregation.** Nothing operational
  depends on `sr0`; the founding document defers the fleet view.

## Design

### A collector of its own

`src/harbor_console/storage.py`, a new module beside the existing ten. It
collects; it does not render. `system.py` keeps its flat
`dict[str, str | float | int]` contract untouched -- a variable-length list
of filesystems does not belong in a dict of scalars, and both renderers want
different densities of the same list.

`disk_utilization` leaves the metrics dict. Root's filesystem is the first
storage entry, with bytes rather than a bare percentage, so nothing is lost.
Per CLAUDE.md, that key's removal touches the collector, both renderers and
`tests/test_system.py` together.

### The contract

```python
@dataclass(frozen=True)
class StorageEntry:
    label: str           # "/", "/home/arm/media", "VG ubuntu-vg", "/mnt/nas/photos", "sdb"
    used: int | None     # bytes; None when not measurable
    total: int | None
    percent: float | None
    note: str            # "" when measured, else why not
```

Numbers where they exist, a reason where they do not, in three shapes a
renderer distinguishes without judgement of its own:

| Fields set | Rendered as | Example |
|---|---|---|
| `used`, `total`, `percent` | `format_usage(...)`, the existing helper | `65.0 / 98.0 GiB (70.0%)` |
| `total` only | size, then the note | `233.9 GiB unallocated` |
| neither | the note alone | `remote -- not measured` |

The middle shape is why `total` and `used` are separately optional: volume-group
slack and an unmounted disk both have a real size and no meaningful "used", and
losing the size would throw away the only number that makes them worth a row.

The five notes, each standing for a fact rather than a gap:

| `note` | Means |
|---|---|
| `remote -- not measured` | A network mount, enumerated but never `statvfs`ed |
| `unallocated` | Volume-group slack, derived from `lsblk` |
| `no filesystem mounted` | A block device nothing is using |
| `unavailable` | `disk_usage` failed on this one mount |
| `8 snap mounts (squashfs, read-only)` | The collapsed image-mount line |

The last one is the one compromise with "show everything, both surfaces".
Eight rows of read-only 100%-full squashfs is noise that would push the real
filesystems off a login console; one line keeps them visible and countable
without pretending they are storage anyone can act on. It is a summary, not
a filter -- the count is the evidence that nothing was dropped silently,
which is the distinction ADR 18 turns on.

### Functions

```python
REMOTE_FSTYPES = frozenset({"cifs", "smb3", "nfs", "nfs4", "fuse.sshfs"})
LSBLK_TIMEOUT_SECONDS = 5.0

def local_filesystems(partitions=psutil.disk_partitions, usage=psutil.disk_usage) -> list[StorageEntry]
def remote_mounts(mounts_path="/proc/mounts") -> list[StorageEntry]
def block_devices(run=subprocess.run, timeout=LSBLK_TIMEOUT_SECONDS) -> list[StorageEntry]
def collect_storage(...) -> tuple[StorageEntry, ...]
```

Every source is injectable, so the tests see no real disk, no real
`/proc/mounts` and no real `lsblk`, exactly as `app.run()` takes its
collector and sleep.

- **`local_filesystems`** reads `psutil.disk_partitions(all=False)` and
  diverts image mounts to the collapsed line, matched by fstype
  (`squashfs`, `erofs`) rather than by the read-only flag -- a legitimately
  read-only ext4 or vfat mount is storage someone may need to see, and on
  hpz440 `/boot/efi` reports `ro` among its options.
  It also drops any partition whose fstype is in `REMOTE_FSTYPES` *before*
  calling `disk_usage` on it. psutil's `all=False` already omits those on
  Linux -- they are `nodev` filesystems, and the two CIFS mounts are verified
  absent from its output on hpz440 -- but that is an implementation detail of
  a dependency, and the promise that nothing here can block on a dead server
  should be this module's own. `remote_mounts` is then the single source of
  remote rows, so a mount cannot appear twice: anything matched by
  `REMOTE_FSTYPES` leaves `local_filesystems` unmeasured and comes back from
  `/proc/mounts` named and unmeasured.
  Each `disk_usage` call is guarded on its own: one unreadable mount becomes
  `unavailable` and the other rows survive.
- **`remote_mounts`** reads `/proc/mounts` directly rather than
  `disk_partitions(all=True)` -- 76 entries of mostly kernel noise there,
  and the CIFS mounts are absent from the default list anyway. Matches on
  fstype (`cifs`, `nfs`, `nfs4`, `smbfs`, `fuse.sshfs`). Never measured.
- **`block_devices`** runs `lsblk -J -b -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS`
  under an explicit timeout, the way every other subprocess collector here
  does (`DOCKER_TIMEOUT_SECONDS` in `system.py`, and its pair in
  `docker.py`). A stalled device or udev interaction is exactly the hazard
  the CIFS rule above exists for, and an external command with no deadline
  reintroduces it on both refresh paths. It walks the tree twice: once for `LVM2_member` devices, emitting PV size
  minus the sum of its LV children as slack; once for devices of type `disk`
  or `part` with no mountpoint, no children, no LVM or RAID membership, and
  `rom` excluded.
- **`collect_storage`** concatenates in display order: local filesystems
  (root first, then by mountpoint), volume-group slack, stray devices,
  remote mounts, the snap line.

### Degradation

Collectors never raise on a hostile environment, per CLAUDE.md:

- `lsblk` missing, failing, exceeding `LSBLK_TIMEOUT_SECONDS`, or returning
  output that is not the expected JSON yields no block entries, the way
  `get_docker_container_count()` yields `0` -- `TimeoutExpired`,
  `FileNotFoundError` and `JSONDecodeError` all land in the same place.
- A single mount that cannot be measured yields `unavailable` for that row.
- `/proc/mounts` unreadable yields one entry labelled `remote mounts` with
  note `unavailable`, not an empty list, for the same reason
  `disk_partitions` failing does: nothing found and nothing looked at must
  not render alike. A single malformed record inside a readable file is
  skipped on its own, so one short line does not cost the mounts around it.
- `disk_partitions` itself failing yields one entry labelled `storage` with
  note `unavailable` -- not an empty list. An empty list would be
  indistinguishable from a host with no storage, and ADR 18 is precisely
  about absence of evidence never looking like evidence of absence.

### Renderers

`ui.py` replaces its single Disk row with one row per entry. `web.py` grows
its own Storage table below the host table; six or more rows do not belong
among host vitals like uptime and IPv4. Neither formats anything the
collector did not hand it.

### Wiring

Both paths get the collector, not just the console:

- `app.run()` gains a `storage_collector` parameter beside `collector`,
  defaulting to the real implementation, and passes the entries to
  `build_dashboard(metrics, storage)`.
- `webapp.collect_snapshot()` gains a `storage=collect_storage` parameter
  alongside its existing `collector`, `listeners`, `containers`, `routers`
  and `prober`, and calls it once per cycle. `_default_prober` keeps taking
  the default. Without this the web renderer would be handed an empty tuple
  every cycle and its new Storage table would render blank while the console
  showed the real thing -- the two surfaces are one core, and a field on
  `Snapshot` that only one path populates is not wiring, it is a
  discrepancy.
- `Snapshot` gains `storage: tuple[StorageEntry, ...]`, defaulting to `()`
  so `starting_snapshot()` stays honest about a cycle that has not run. No
  `storage_available` flag: the notes carry that in-band and every host has a
  root filesystem.
- `webapp`'s degraded stub drops `disk_utilization` along with the key.

## Testing

TDD, one behaviour at a time. New `tests/test_storage.py` covers:

- a measured filesystem, and the `used / total GiB (percent)` shape matching
  memory and swap
- one mount whose `disk_usage` raises, with the other rows intact
- squashfs mounts collapsing to one counted line
- CIFS mounts listed and never passed to `disk_usage` (asserted by a fake
  that fails the test if called for them)
- volume-group slack derived from captured `lsblk -J -b` output from hpz440
  -- the real tree, with the LVM pair, the eight snap loops and the three
  optical drives
- a stray disk with no mountpoint, and the exclusions: LVM members, parents
  with children, `rom`
- `lsblk` absent, `lsblk` returning garbage, `lsblk` exceeding its timeout,
  `disk_partitions` raising, `/proc/mounts` unreadable, and one malformed
  `/proc/mounts` record among good ones
- a remote fstype handed back by a fake `disk_partitions`, proving
  `local_filesystems` drops it before measuring rather than relying on
  psutil's default, and that it appears exactly once in `collect_storage`
- `collect_snapshot` populating `Snapshot.storage` from an injected fake, so
  the web path cannot silently render an empty table

Updated: `test_system.py` (key removed), `test_ui.py`, `test_web.py`,
`test_webapp.py`, `test_app.py`.

## Consequences

- The console reports the storage the host actually has. On hpz440 that is
  four filesystems, 233.9 GiB of volume-group slack, two NAS mounts named but
  not measured, and a snap count -- against one number today.
- `collect_system_metrics()` loses a key for the first time since the web
  surface shipped. The dict stays the contract for scalars; lists live
  beside it.
- The tty1 panel grows by roughly five rows on this host. If it ever
  overflows a real console, that is a display decision to make then, with the
  overflow in hand, rather than a cap guessed at now.
