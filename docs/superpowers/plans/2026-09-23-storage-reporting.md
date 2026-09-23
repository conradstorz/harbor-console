# Storage Reporting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single `disk_utilization` percentage with a collector that reports every filesystem, the volume-group slack, stray block devices and named-but-unmeasured network mounts, on both surfaces.

**Architecture:** One new collector module, `storage.py`, over two sources — `psutil` for mounted filesystems and `lsblk -J -b` for the block layer. It returns a list of `StorageEntry`, which carries numbers where they exist and a reason where they don't. `system.py` keeps its flat scalar-dict contract and loses the `disk_utilization` key; both renderers iterate the list and format it with one shared helper.

**Tech Stack:** Python 3.13+, `psutil`, stdlib `subprocess`/`json`, `rich` for tty1, stdlib `http.server` for the page, `pytest`.

**Spec:** `docs/superpowers/specs/2026-09-23-storage-reporting-design.md`

## Global Constraints

- Run everything with `uv`: `uv run pytest`, never `pip` or `python -m venv`.
- TDD: write the failing test, watch it fail, then implement. Commit each task.
- Collectors never raise on a hostile environment (CLAUDE.md, "Graceful degradation"). `tailnet.py` is the only exception and this is not it.
- Every external source is injectable, so tests touch no real disk, no real `/proc/mounts` and no real `lsblk` — the pattern `app.run()` and `docker.running_containers()` already use.
- No new runtime dependency. Stdlib plus the `psutil` already in use.
- Formatting helpers live beside the collector, as `format_usage` does in `system.py`. Renderers format nothing the collector didn't hand them.
- `LSBLK_TIMEOUT_SECONDS = 5.0`, matching the explicit-deadline convention of `DOCKER_TIMEOUT_SECONDS = 2.0` (`system.py:78`) and `DOCKER_INSPECT_TIMEOUT_SECONDS = 5.0` (`docker.py:52`).
- `REMOTE_FSTYPES = frozenset({"cifs", "smb3", "nfs", "nfs4", "fuse.sshfs"})`, `IMAGE_FSTYPES = frozenset({"squashfs", "erofs"})`.

## Two deliberate deviations from the spec

Both are structural, neither changes behaviour the spec describes:

1. **The collapsed image-mount line gets its own function, `image_mounts()`,** rather than being appended inside `local_filesystems()`. The spec requires that line to render *last*, after slack, stray devices and remote mounts; a single function returning both would put it in the middle of `collect_storage`'s concatenation. Cost: `disk_partitions` is called twice per cycle, which is a `/proc` read.
2. **The image-mount note is not snap-specific.** The spec's example reads `8 snap mounts (squashfs, read-only)`; the implementation says `8 image mounts (squashfs, read-only)`, because nothing in the code knows snapd produced them.

## File Structure

| File | Responsibility |
|---|---|
| Create: `src/harbor_console/storage.py` | Collects storage. `StorageEntry`, `format_entry`, four collectors, one assembler. No rendering, no policy. |
| Create: `tests/test_storage.py` | Every branch of the above against fakes. |
| Modify: `src/harbor_console/snapshot.py` | `Snapshot.storage` field. |
| Modify: `src/harbor_console/webapp.py` | `collect_snapshot` gains the collector; the degraded stub drops the dead key. |
| Modify: `src/harbor_console/web.py` | A Storage section; the host table's Disk row goes. |
| Modify: `src/harbor_console/ui.py` | One row per entry instead of the Disk row. |
| Modify: `src/harbor_console/app.py` | `storage_collector` injection, passed to the renderer. |
| Modify: `src/harbor_console/system.py` | `disk_utilization` removed. |
| Modify: `tests/test_system.py`, `tests/test_ui.py`, `tests/test_web.py`, `tests/test_webapp.py`, `tests/test_app.py` | Follow the contract change. |

---

### Task 1: `StorageEntry`, `format_entry`, and mounted filesystems

**Files:**
- Create: `src/harbor_console/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: `harbor_console.system.format_bytes`, `harbor_console.system.format_usage`.
- Produces: `StorageEntry(label, used, total, percent, note)`; `format_entry(entry) -> str`; `local_filesystems(partitions=psutil.disk_partitions, usage=psutil.disk_usage) -> list[StorageEntry]`; `image_mounts(partitions=psutil.disk_partitions) -> list[StorageEntry]`; the constants `REMOTE_FSTYPES`, `IMAGE_FSTYPES`, `NOTE_UNAVAILABLE`, `NOTE_REMOTE`, `NOTE_UNALLOCATED`, `NOTE_NO_FILESYSTEM`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_storage.py`:

```python
from types import SimpleNamespace

from harbor_console.storage import (
    NOTE_UNAVAILABLE,
    StorageEntry,
    format_entry,
    image_mounts,
    local_filesystems,
)


def part(device, mountpoint, fstype, opts="rw,relatime"):
    return SimpleNamespace(device=device, mountpoint=mountpoint, fstype=fstype, opts=opts)


def usage(used, total, percent):
    return SimpleNamespace(used=used, total=total, percent=percent, free=total - used)


GIB = 1024**3


def test_format_entry_measured_reads_like_memory_and_swap():
    entry = StorageEntry(label="/", used=65 * GIB, total=98 * GIB, percent=70.0)

    assert format_entry(entry) == "65.0 / 98.0 GiB (70.0%)"


def test_format_entry_size_only_keeps_the_size_and_names_the_reason():
    entry = StorageEntry(label="VG ubuntu-vg", total=200 * GIB, note="unallocated")

    assert format_entry(entry) == "200.0 GiB unallocated"


def test_format_entry_without_numbers_is_the_note_alone():
    entry = StorageEntry(label="/mnt/nas/photos", note="remote -- not measured")

    assert format_entry(entry) == "remote -- not measured"


def test_local_filesystems_measures_every_mount_root_first():
    partitions = lambda all=False: [
        part("/dev/sda2", "/boot", "ext4"),
        part("/dev/mapper/vg-root", "/", "ext4"),
    ]
    sizes = {"/": usage(65 * GIB, 98 * GIB, 70.0), "/boot": usage(1 * GIB, 2 * GIB, 50.0)}

    entries = local_filesystems(partitions=partitions, usage=lambda mp: sizes[mp])

    assert [e.label for e in entries] == ["/", "/boot"]
    assert format_entry(entries[0]) == "65.0 / 98.0 GiB (70.0%)"


def test_local_filesystems_survives_one_unmeasurable_mount():
    partitions = lambda all=False: [
        part("/dev/mapper/vg-root", "/", "ext4"),
        part("/dev/sdz1", "/mnt/broken", "ext4"),
    ]

    def flaky(mountpoint):
        if mountpoint == "/mnt/broken":
            raise PermissionError("nope")
        return usage(65 * GIB, 98 * GIB, 70.0)

    entries = local_filesystems(partitions=partitions, usage=flaky)

    assert [e.label for e in entries] == ["/", "/mnt/broken"]
    assert entries[1].note == NOTE_UNAVAILABLE
    assert entries[1].total is None


def test_local_filesystems_never_measures_a_remote_mount():
    """psutil's all=False already omits these; the guarantee is ours anyway."""
    partitions = lambda all=False: [
        part("/dev/mapper/vg-root", "/", "ext4"),
        part("//nas/photo", "/mnt/nas/photos", "cifs"),
    ]
    measured = []

    def recording(mountpoint):
        measured.append(mountpoint)
        return usage(65 * GIB, 98 * GIB, 70.0)

    entries = local_filesystems(partitions=partitions, usage=recording)

    assert measured == ["/"]
    assert [e.label for e in entries] == ["/"]


def test_local_filesystems_reports_unavailable_when_it_cannot_enumerate():
    def exploding(all=False):
        raise OSError("no /proc")

    entries = local_filesystems(partitions=exploding, usage=lambda _mp: None)

    assert len(entries) == 1
    assert entries[0].label == "storage"
    assert entries[0].note == NOTE_UNAVAILABLE


def test_image_mounts_collapse_to_one_counted_line():
    partitions = lambda all=False: [
        part("/dev/mapper/vg-root", "/", "ext4"),
        part("/dev/loop0", "/snap/core20/2866", "squashfs", opts="ro"),
        part("/dev/loop1", "/snap/lxd/40575", "squashfs", opts="ro"),
    ]

    entries = image_mounts(partitions=partitions)

    assert len(entries) == 1
    assert format_entry(entries[0]) == "2 image mounts (squashfs, read-only)"


def test_image_mounts_are_absent_when_there_are_none():
    partitions = lambda all=False: [part("/dev/mapper/vg-root", "/", "ext4")]

    assert image_mounts(partitions=partitions) == []


def test_read_only_real_filesystems_are_not_collapsed():
    """/boot/efi reports `ro` among its options on hpz440 and is real storage."""
    partitions = lambda all=False: [part("/dev/sda1", "/boot/efi", "vfat", opts="ro,relatime")]

    entries = local_filesystems(
        partitions=partitions, usage=lambda _mp: usage(6 * GIB // 1000, 1 * GIB, 1.0)
    )

    assert [e.label for e in entries] == ["/boot/efi"]
    assert image_mounts(partitions=partitions) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_storage.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'harbor_console.storage'`.

- [ ] **Step 3: Write the implementation**

Create `src/harbor_console/storage.py`:

```python
"""What storage this host has: filesystems, volume-group slack, stray devices.

Collects only. Two sources: `psutil` for mounted filesystems, and `lsblk` for
the block layer underneath them -- the volume-group slack `df` cannot see, and
devices nothing has mounted.

An entry carries numbers where they exist and a reason where they do not, so a
renderer never decides which case it is looking at. Network mounts are named
and never measured: `statvfs` on an unreachable CIFS mount blocks, and blocking
is fatal to a 1 Hz refresh loop and to the web prober.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psutil

from harbor_console.system import format_bytes, format_usage

#: Never measured -- see the module docstring. `remote_mounts` names them from
#: /proc/mounts instead, and `local_filesystems` drops them before any
#: `disk_usage` call. psutil's `all=False` already omits them on Linux (they are
#: nodev filesystems), but the promise that nothing here blocks on a dead server
#: is this module's own rather than a dependency's implementation detail.
REMOTE_FSTYPES = frozenset({"cifs", "smb3", "nfs", "nfs4", "fuse.sshfs"})

#: Read-only image mounts -- snaps and the like. Matched by fstype rather than
#: by the read-only flag: `/boot/efi` reports `ro` among its options and is real
#: storage someone may need to see.
IMAGE_FSTYPES = frozenset({"squashfs", "erofs"})

#: A bound on `lsblk`, which runs on both refresh paths. A stalled device or
#: udev interaction is the same hazard the remote-mount rule exists for.
LSBLK_TIMEOUT_SECONDS = 5.0

NOTE_UNAVAILABLE = "unavailable"
NOTE_REMOTE = "remote -- not measured"
NOTE_UNALLOCATED = "unallocated"
NOTE_NO_FILESYSTEM = "no filesystem mounted"


@dataclass(frozen=True)
class StorageEntry:
    """One thing that holds bytes, measured or merely named."""

    label: str
    used: int | None = None
    total: int | None = None
    percent: float | None = None
    note: str = ""


def format_entry(entry: StorageEntry) -> str:
    """The three shapes an entry renders as, and nothing else.

    Measured reads exactly like the memory and swap rows, through the same
    helper. An entry with a size and no meaningful "used" -- volume-group slack,
    an unmounted disk -- keeps its size, because that is the only number that
    makes it worth a row.
    """
    if entry.used is not None and entry.total is not None and entry.percent is not None:
        return format_usage(entry.used, entry.total, entry.percent)
    if entry.total is not None:
        return f"{format_bytes(entry.total)} GiB {entry.note}".strip()
    return entry.note


def local_filesystems(
    partitions: Callable[..., object] = psutil.disk_partitions,
    usage: Callable[[str], object] = psutil.disk_usage,
) -> list[StorageEntry]:
    """Every mounted local filesystem, root first, then by mountpoint.

    Each `usage` call is guarded on its own: one unreadable mount becomes
    `unavailable` and the rows around it survive. Failing to enumerate at all
    yields one `unavailable` entry rather than an empty list -- nothing found
    and nothing looked at must not render alike (ADR 18).
    """
    try:
        found = list(partitions(all=False))
    except Exception:
        return [StorageEntry(label="storage", note=NOTE_UNAVAILABLE)]

    entries: list[StorageEntry] = []
    for p in found:
        fstype = getattr(p, "fstype", "")
        if fstype in REMOTE_FSTYPES or fstype in IMAGE_FSTYPES:
            continue
        mountpoint = getattr(p, "mountpoint", "")
        try:
            measured = usage(mountpoint)
            entries.append(
                StorageEntry(
                    label=mountpoint,
                    used=int(measured.used),  # type: ignore[attr-defined]
                    total=int(measured.total),  # type: ignore[attr-defined]
                    percent=float(measured.percent),  # type: ignore[attr-defined]
                )
            )
        except Exception:
            entries.append(StorageEntry(label=mountpoint, note=NOTE_UNAVAILABLE))
    entries.sort(key=lambda e: (e.label != "/", e.label))
    return entries


def image_mounts(
    partitions: Callable[..., object] = psutil.disk_partitions,
) -> list[StorageEntry]:
    """One counted line for every read-only image mount, or nothing.

    A row each would push the real filesystems off a login console. The count is
    what keeps this a summary rather than a filter: it is the evidence that
    nothing was dropped silently.
    """
    try:
        found = list(partitions(all=False))
    except Exception:
        return []
    count = sum(1 for p in found if getattr(p, "fstype", "") in IMAGE_FSTYPES)
    if not count:
        return []
    return [
        StorageEntry(
            label="Image mounts", note=f"{count} image mounts (squashfs, read-only)"
        )
    ]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_storage.py -q`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/storage.py tests/test_storage.py
git commit -m "feat(storage): collect mounted filesystems with bytes, not a bare percent"
```

---

### Task 2: Network mounts, named and never measured

**Files:**
- Modify: `src/harbor_console/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: `REMOTE_FSTYPES`, `StorageEntry`, `NOTE_REMOTE`, `NOTE_UNAVAILABLE` from Task 1.
- Produces: `remote_mounts(mounts_path="/proc/mounts") -> list[StorageEntry]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_storage.py`, moving the new imports up beside the
existing ones at the top of the file rather than leaving them mid-module:

```python
from harbor_console.storage import NOTE_REMOTE, remote_mounts

MOUNTS = """\
/dev/mapper/vg-root / ext4 rw,relatime 0 0
//nas/photo /mnt/nas/photos cifs rw,relatime,vers=3.1.1 0 0
//nas/Photos-Organized /mnt/nas/organized cifs rw,relatime 0 0
tmpfs /run tmpfs rw,nosuid 0 0
nas:/export /mnt/nfs nfs4 rw 0 0
"""


def test_remote_mounts_names_network_mounts_and_nothing_else(tmp_path):
    path = tmp_path / "mounts"
    path.write_text(MOUNTS)

    entries = remote_mounts(mounts_path=str(path))

    assert [e.label for e in entries] == [
        "/mnt/nas/photos",
        "/mnt/nas/organized",
        "/mnt/nfs",
    ]
    assert all(e.note == NOTE_REMOTE for e in entries)
    assert all(e.total is None and e.used is None for e in entries)


def test_remote_mounts_unescapes_octal_spaces(tmp_path):
    path = tmp_path / "mounts"
    path.write_text("//nas/share /mnt/my\\040photos cifs rw 0 0\n")

    entries = remote_mounts(mounts_path=str(path))

    assert [e.label for e in entries] == ["/mnt/my photos"]


def test_remote_mounts_skips_one_malformed_record_and_keeps_the_rest(tmp_path):
    path = tmp_path / "mounts"
    path.write_text("garbage\n//nas/photo /mnt/nas/photos cifs rw 0 0\n\n")

    entries = remote_mounts(mounts_path=str(path))

    assert [e.label for e in entries] == ["/mnt/nas/photos"]


def test_remote_mounts_unreadable_is_unavailable_not_empty(tmp_path):
    entries = remote_mounts(mounts_path=str(tmp_path / "absent"))

    assert len(entries) == 1
    assert entries[0].label == "remote mounts"
    assert entries[0].note == NOTE_UNAVAILABLE
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_storage.py -q`
Expected: `ImportError: cannot import name 'remote_mounts'`.

- [ ] **Step 3: Write the implementation**

Append to `src/harbor_console/storage.py`:

```python
def remote_mounts(mounts_path: str = "/proc/mounts") -> list[StorageEntry]:
    """Network mounts, named from /proc/mounts and never measured.

    Read directly rather than through `disk_partitions(all=True)`, which on
    hpz440 answers 76 entries of mostly kernel noise. Enumerating is safe;
    measuring is what hangs on a dead server.

    An unreadable file yields one `unavailable` entry rather than an empty list,
    for the same reason `local_filesystems` does. A single malformed record
    inside a readable file is skipped on its own, so one short line does not
    cost the mounts around it.
    """
    try:
        text = Path(mounts_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [StorageEntry(label="remote mounts", note=NOTE_UNAVAILABLE)]

    entries: list[StorageEntry] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        mountpoint, fstype = fields[1], fields[2]
        if fstype not in REMOTE_FSTYPES:
            continue
        # /proc/mounts escapes a space in a path as \040.
        entries.append(
            StorageEntry(label=mountpoint.replace("\\040", " "), note=NOTE_REMOTE)
        )
    return entries
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_storage.py -q`
Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/storage.py tests/test_storage.py
git commit -m "feat(storage): name network mounts without measuring them"
```

---

### Task 3: The block layer — volume-group slack and stray devices

**Files:**
- Modify: `src/harbor_console/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: `StorageEntry`, `NOTE_UNALLOCATED`, `NOTE_NO_FILESYSTEM`, `LSBLK_TIMEOUT_SECONDS` from Task 1.
- Produces: `block_devices(run=subprocess.run, timeout=LSBLK_TIMEOUT_SECONDS) -> list[StorageEntry]`, returning slack entries first, then stray devices.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_storage.py`, again lifting the imports to the top of the
file. `LSBLK_TREE` is the real shape captured from hpz440, trimmed to two snap
loops:

```python
import json
import subprocess

from harbor_console.storage import (
    LSBLK_TIMEOUT_SECONDS,
    NOTE_NO_FILESYSTEM,
    NOTE_UNALLOCATED,
    block_devices,
)

LSBLK_TREE = {
    "blockdevices": [
        {
            "name": "loop0",
            "type": "loop",
            "size": 66879488,
            "fstype": "squashfs",
            "mountpoints": ["/snap/core20/2866"],
        },
        {
            "name": "loop1",
            "type": "loop",
            "size": 66883584,
            "fstype": "squashfs",
            "mountpoints": ["/snap/core20/2922"],
        },
        {
            "name": "sda",
            "type": "disk",
            "size": 3000592982016,
            "fstype": None,
            "mountpoints": [None],
            "children": [
                {
                    "name": "sda1",
                    "type": "part",
                    "size": 1127219200,
                    "fstype": "vfat",
                    "mountpoints": ["/boot/efi"],
                },
                {
                    "name": "sda2",
                    "type": "part",
                    "size": 2147483648,
                    "fstype": "ext4",
                    "mountpoints": ["/boot"],
                },
                {
                    "name": "sda3",
                    "type": "part",
                    "size": 2997315698688,
                    "fstype": "LVM2_member",
                    "mountpoints": [None],
                    "children": [
                        {
                            "name": "ubuntu--vg-ubuntu--lv",
                            "type": "lvm",
                            "size": 107374182400,
                            "fstype": "ext4",
                            "mountpoints": ["/"],
                        },
                        {
                            "name": "ubuntu--vg-media",
                            "type": "lvm",
                            "size": 2638829584384,
                            "fstype": "ext4",
                            "mountpoints": ["/home/arm/media"],
                        },
                    ],
                },
            ],
        },
        {"name": "sr0", "type": "rom", "size": 8046176256, "fstype": "udf", "mountpoints": [None]},
        {"name": "sr1", "type": "rom", "size": 7909691392, "fstype": "udf", "mountpoints": [None]},
    ]
}


def fake_lsblk(tree=None, stdout=None, returncode=0, raises=None):
    """Stands in for subprocess.run; records the argv it was handed."""
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if raises is not None:
            raise raises
        text = stdout if stdout is not None else json.dumps(tree or {})
        return SimpleNamespace(stdout=text, returncode=returncode)

    run.calls = calls
    return run


def test_block_devices_reports_volume_group_slack():
    entries = block_devices(run=fake_lsblk(LSBLK_TREE))

    slack = [e for e in entries if e.note == NOTE_UNALLOCATED]
    assert len(slack) == 1
    assert slack[0].label == "VG ubuntu-vg"
    # 2997315698688 - (107374182400 + 2638829584384)
    assert slack[0].total == 251111931904
    assert slack[0].used is None
    assert format_entry(slack[0]) == "233.9 GiB unallocated"


def test_block_devices_excludes_lvm_members_parents_and_optical():
    entries = block_devices(run=fake_lsblk(LSBLK_TREE))

    labels = [e.label for e in entries]
    assert labels == ["VG ubuntu-vg"]
    for excluded in ("sda", "sda3", "sr0", "sr1", "loop0"):
        assert excluded not in labels


def test_block_devices_reports_a_stray_disk():
    tree = {
        "blockdevices": [
            {
                "name": "sdb",
                "type": "disk",
                "size": 2000398934016,
                "fstype": None,
                "mountpoints": [None],
            }
        ]
    }

    entries = block_devices(run=fake_lsblk(tree))

    assert [e.label for e in entries] == ["sdb"]
    assert entries[0].note == NOTE_NO_FILESYSTEM
    assert entries[0].total == 2000398934016


def test_block_devices_passes_an_explicit_timeout():
    run = fake_lsblk(LSBLK_TREE)

    block_devices(run=run)

    args, kwargs = run.calls[0]
    assert args[:2] == ["lsblk", "-J"]
    assert kwargs["timeout"] == LSBLK_TIMEOUT_SECONDS


def test_block_devices_degrades_on_timeout():
    run = fake_lsblk(raises=subprocess.TimeoutExpired(cmd="lsblk", timeout=5.0))

    assert block_devices(run=run) == []


def test_block_devices_degrades_when_lsblk_is_missing():
    run = fake_lsblk(raises=FileNotFoundError("lsblk"))

    assert block_devices(run=run) == []


def test_block_devices_degrades_on_garbage_output():
    assert block_devices(run=fake_lsblk(stdout="not json at all")) == []


def test_block_devices_degrades_on_nonzero_exit():
    assert block_devices(run=fake_lsblk(LSBLK_TREE, returncode=1)) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_storage.py -q`
Expected: `ImportError: cannot import name 'block_devices'`.

- [ ] **Step 3: Write the implementation**

Append to `src/harbor_console/storage.py`:

```python
#: A device-mapper name escapes a literal hyphen as `--`, and separates the
#: volume group from the logical volume with a single one: `ubuntu--vg-ubuntu--lv`
#: is `ubuntu-vg` / `ubuntu-lv`.
_DM_SEPARATOR = re.compile(r"(?<!-)-(?!-)")

#: A device carrying one of these is in use by a layer above it, so it is not a
#: stray even with nothing mounted on it.
_MEMBER_FSTYPES = frozenset({"LVM2_member", "linux_raid_member"})

#: Devices that can hold a filesystem at all. `loop` is a mounted image and
#: `rom` is an optical drive -- neither is storage anyone allocates.
_STRAY_TYPES = frozenset({"disk", "part"})


def _mounted(node: dict) -> bool:
    return any(mp for mp in node.get("mountpoints") or [])


def _vg_name(dm_name: str) -> str | None:
    """The volume group a logical volume belongs to, from its dm name."""
    parts = _DM_SEPARATOR.split(dm_name, maxsplit=1)
    if len(parts) != 2:
        return None
    return parts[0].replace("--", "-")


def _walk(nodes: list) -> list[dict]:
    flat: list[dict] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        flat.append(node)
        flat.extend(_walk(node.get("children") or []))
    return flat


def block_devices(
    run: Callable[..., object] = subprocess.run,
    timeout: float = LSBLK_TIMEOUT_SECONDS,
) -> list[StorageEntry]:
    """Volume-group slack, then devices nothing has mounted.

    Slack is derived rather than asked for: `vgs` needs root and this process
    runs as the unprivileged `harbor` user (ADR 5), so the figure is the
    physical volume's size minus the sum of its logical volumes. It is
    approximate by a few mebibytes of LVM metadata and is the only way to see,
    without privileges, whether a filesystem has room to grow.

    Every failure is the same outage and yields no entries, the way
    `get_docker_container_count()` yields `0`: a missing binary, a non-zero
    exit, a timeout, or output that is not the JSON shape expected.
    """
    try:
        completed = run(
            ["lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if getattr(completed, "returncode", 1) != 0:
        return []
    try:
        tree = json.loads(getattr(completed, "stdout", "") or "")
    except (TypeError, ValueError):
        return []
    if not isinstance(tree, dict) or not isinstance(tree.get("blockdevices"), list):
        return []

    slack: list[StorageEntry] = []
    stray: list[StorageEntry] = []
    for node in _walk(tree["blockdevices"]):
        children = [c for c in (node.get("children") or []) if isinstance(c, dict)]
        fstype = node.get("fstype") or ""
        size = node.get("size")
        name = node.get("name") or ""
        if not isinstance(size, int):
            continue
        if fstype == "LVM2_member" and children:
            allocated = sum(c["size"] for c in children if isinstance(c.get("size"), int))
            group = next(
                (vg for vg in (_vg_name(c.get("name") or "") for c in children) if vg),
                None,
            )
            slack.append(
                StorageEntry(
                    label=f"VG {group}" if group else name,
                    total=max(size - allocated, 0),
                    note=NOTE_UNALLOCATED,
                )
            )
            continue
        if (
            node.get("type") in _STRAY_TYPES
            and not children
            and not _mounted(node)
            and fstype not in _MEMBER_FSTYPES
        ):
            stray.append(StorageEntry(label=name, total=size, note=NOTE_NO_FILESYSTEM))
    return slack + stray
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_storage.py -q`
Expected: 22 passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/storage.py tests/test_storage.py
git commit -m "feat(storage): derive volume-group slack and stray devices from lsblk"
```

---

### Task 4: `collect_storage`, the assembler

**Files:**
- Modify: `src/harbor_console/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: `local_filesystems`, `block_devices`, `remote_mounts`, `image_mounts`.
- Produces: `collect_storage(filesystems=local_filesystems, blocks=block_devices, remote=remote_mounts, images=image_mounts) -> tuple[StorageEntry, ...]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_storage.py`:

```python
from harbor_console.storage import collect_storage


def test_collect_storage_orders_filesystems_slack_stray_remote_then_images():
    entries = collect_storage(
        filesystems=lambda: [StorageEntry(label="/", used=1, total=2, percent=50.0)],
        blocks=lambda: [
            StorageEntry(label="VG vg0", total=3, note=NOTE_UNALLOCATED),
            StorageEntry(label="sdb", total=4, note=NOTE_NO_FILESYSTEM),
        ],
        remote=lambda: [StorageEntry(label="/mnt/nas", note=NOTE_REMOTE)],
        images=lambda: [StorageEntry(label="Image mounts", note="2 image mounts")],
    )

    assert [e.label for e in entries] == ["/", "VG vg0", "sdb", "/mnt/nas", "Image mounts"]
    assert isinstance(entries, tuple)


def test_collect_storage_defaults_touch_the_real_host_without_raising():
    """The release criterion: a collector never raises on a hostile environment."""
    assert isinstance(collect_storage(), tuple)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_storage.py -q`
Expected: `ImportError: cannot import name 'collect_storage'`.

- [ ] **Step 3: Write the implementation**

Append to `src/harbor_console/storage.py`:

```python
def collect_storage(
    filesystems: Callable[[], list[StorageEntry]] = local_filesystems,
    blocks: Callable[[], list[StorageEntry]] = block_devices,
    remote: Callable[[], list[StorageEntry]] = remote_mounts,
    images: Callable[[], list[StorageEntry]] = image_mounts,
) -> tuple[StorageEntry, ...]:
    """Every storage entry this host has, in display order.

    Measured filesystems first, because they are what anyone came to read; then
    the layer underneath them, then what is merely named, then the collapsed
    image-mount line last.
    """
    return (*filesystems(), *blocks(), *remote(), *images())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_storage.py -q`
Expected: 24 passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/storage.py tests/test_storage.py
git commit -m "feat(storage): assemble every entry in display order"
```

---

### Task 5: The web surface shows storage

Done before the tty1 task so no commit ever leaves the page with less storage information than it has now: `disk_utilization` stays until Task 6.

**Files:**
- Modify: `src/harbor_console/snapshot.py:47` (after `tailnet_address`)
- Modify: `src/harbor_console/webapp.py:116-125` (`collect_snapshot` signature) and its `Snapshot(...)` construction
- Modify: `src/harbor_console/web.py:67` (section order) and a new `_storage_section`
- Test: `tests/test_web.py`, `tests/test_webapp.py`

**Interfaces:**
- Consumes: `collect_storage`, `StorageEntry`, `format_entry` from Tasks 1–4.
- Produces: `Snapshot.storage: tuple[StorageEntry, ...] = ()`; `collect_snapshot(..., storage=collect_storage)`; `web._storage_section(snapshot) -> str`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web.py`, using its existing `snapshot(**overrides)` helper
(line 35) and `web.render_page`, not a hand-built `Snapshot`:

```python
from harbor_console.storage import StorageEntry

STORAGE = (
    StorageEntry(label="/", used=65 * 1024**3, total=98 * 1024**3, percent=70.0),
    StorageEntry(label="VG ubuntu-vg", total=251111931904, note="unallocated"),
    StorageEntry(label="/mnt/nas/photos", note="remote -- not measured"),
)


def test_page_shows_every_storage_entry():
    page = web.render_page(snapshot(storage=STORAGE)).decode()

    assert "Storage" in page
    assert "65.0 / 98.0 GiB (70.0%)" in page
    assert "233.9 GiB unallocated" in page
    assert escape("/mnt/nas/photos") in page
    assert escape("remote -- not measured") in page


def test_storage_before_the_first_cycle_says_so_rather_than_showing_empty():
    page = web.render_page(snapshot(probed=False, storage=())).decode()

    assert "Nothing has been collected yet" in page
```

Add to `tests/test_webapp.py`, using its existing `collect(**overrides)` helper
(line 35), which already supplies every other source:

```python
from harbor_console.storage import StorageEntry


def test_collect_snapshot_populates_storage_from_the_injected_collector():
    entry = StorageEntry(label="/", used=1, total=2, percent=50.0)

    snapshot = collect(storage=lambda: (entry,))

    assert snapshot.storage == (entry,)


def test_collect_snapshot_defaults_to_the_real_storage_collector():
    """The web path must not be the one surface left with an empty table."""
    assert (
        inspect.signature(webapp.collect_snapshot).parameters["storage"].default
        is webapp.collect_storage
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web.py tests/test_webapp.py -q`
Expected: `TypeError: Snapshot.__init__() got an unexpected keyword argument 'storage'` and `collect_snapshot() got an unexpected keyword argument 'storage'`.

- [ ] **Step 3: Write the implementation**

In `src/harbor_console/snapshot.py`, add the import and the field (after `tailnet_address`):

```python
from harbor_console.storage import StorageEntry
```

```python
    #: Every filesystem, volume-group slack, stray device and named-but-
    #: unmeasured network mount. Empty until the first cycle runs, which
    #: `probed` is what distinguishes.
    storage: tuple[StorageEntry, ...] = ()
```

In `src/harbor_console/webapp.py`, import the collector and thread it through:

```python
from harbor_console.storage import StorageEntry, collect_storage
```

Add to `collect_snapshot`'s signature, after `prober`:

```python
    storage: Callable[[], tuple[StorageEntry, ...]] = collect_storage,
```

and pass `storage=storage()` in the `Snapshot(...)` it returns. `_default_prober` needs no change: it takes the default.

In `src/harbor_console/web.py`, add the import, the section, and the call:

```python
from harbor_console.storage import StorageEntry, format_entry
```

```python
def _storage_section(snapshot: Snapshot) -> str:
    """Every filesystem, and the layers around them.

    One row per entry, including the ones with no usage to report: what is
    merely named is still the answer to "what storage does this host have".
    """
    if not snapshot.probed:
        return (
            "<h2>Storage</h2><p>Nothing has been collected yet: the first cycle "
            "has not finished.</p>"
        )
    if not snapshot.storage:
        return "<h2>Storage</h2><p>No storage could be read.</p>"
    rows = "".join(
        f"<tr><td>{escape(entry.label)}</td><td>{escape(format_entry(entry))}</td></tr>"
        for entry in snapshot.storage
    )
    return "<h2>Storage</h2><table>" + rows + "</table>"
```

Insert the call after the host table, at `web.py:67`:

```python
    parts.append(_host_table(snapshot))
    parts.append(_storage_section(snapshot))
    parts.append(_directory_table(snapshot))
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass, 240 + new.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_console/snapshot.py src/harbor_console/webapp.py src/harbor_console/web.py tests/test_web.py tests/test_webapp.py
git commit -m "feat(web): show the host's storage on the status page"
```

---

### Task 6: The console shows storage, and `disk_utilization` goes

**Files:**
- Modify: `src/harbor_console/system.py:122` (remove the key)
- Modify: `src/harbor_console/ui.py:9-20` (signature and rows)
- Modify: `src/harbor_console/app.py:16-31` (injection and call)
- Modify: `src/harbor_console/web.py:85` (drop the Disk row from the host table)
- Modify: `src/harbor_console/webapp.py` (the degraded stub's `disk_utilization: 0.0`)
- Test: `tests/test_system.py:31,45`, `tests/test_ui.py:12`, `tests/test_web.py:24`, `tests/test_webapp.py:20`, `tests/test_app.py`

**Interfaces:**
- Consumes: `collect_storage`, `format_entry`, `StorageEntry`.
- Produces: `build_dashboard(metrics, storage=()) -> Panel`; `app.run(..., storage_collector=collect_storage)`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_ui.py`, delete `"disk_utilization": 78.0,` from `METRICS`, change `render` to take storage, and add the new test:

```python
def render(metrics, storage=()):
    """The panel as text. Wide enough that no cell wraps."""
    console = Console(width=120, record=True)
    console.print(build_dashboard(metrics, storage))
    return console.export_text()


def test_dashboard_shows_one_row_per_storage_entry():
    storage = (
        StorageEntry(label="/", used=65 * 1024**3, total=98 * 1024**3, percent=70.0),
        StorageEntry(label="VG ubuntu-vg", total=251111931904, note="unallocated"),
    )

    page = render(METRICS, storage)

    assert "65.0 / 98.0 GiB (70.0%)" in page
    assert "233.9 GiB unallocated" in page
    assert "Disk utilization" not in page
```

with `from harbor_console.storage import StorageEntry` at the top.

In `tests/test_system.py`, remove the `disk_usage` monkeypatch at line 31 and the `"disk_utilization": 78.0,` entry at line 45, then add:

```python
def test_metrics_no_longer_carry_a_disk_percentage():
    """Storage is a list of its own now -- see storage.py and ADR 18's lesson."""
    assert "disk_utilization" not in collect_system_metrics()
```

In `tests/test_app.py`, assert the storage collector reaches the renderer:

```python
def test_run_passes_storage_to_the_renderer(monkeypatch):
    seen = {}

    def renderer(metrics, storage):
        seen["storage"] = storage
        return "rendered"

    def fake_sleep(_interval):
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "Live", DummyLive)

    result = app.run(
        collector=lambda: {"tick": 1},
        renderer=renderer,
        sleep=fake_sleep,
        storage_collector=lambda: ("entry",),
    )

    assert result == 0
    assert seen["storage"] == ("entry",)
```

and update the existing `renderer(metrics)` in `test_run_updates_dashboard_and_exits_cleanly` to `renderer(metrics, _storage)`.

Delete `"disk_utilization": 3.0,` from the `METRICS` fixtures in `tests/test_web.py:24` and `tests/test_webapp.py:20`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q`
Expected: failures in `test_ui.py` (`build_dashboard() takes 1 positional argument`), `test_app.py` (`run() got an unexpected keyword argument 'storage_collector'`), `test_system.py` (`disk_utilization` still present), and `KeyError: 'disk_utilization'` from `web.py`'s host table.

- [ ] **Step 3: Write the implementation**

`src/harbor_console/system.py` — delete the line:

```python
        "disk_utilization": psutil.disk_usage("/").percent,
```

`src/harbor_console/ui.py` — take the entries and render one row each:

```python
from harbor_console.storage import StorageEntry, format_entry


def build_dashboard(
    metrics: dict[str, str | float | int],
    storage: tuple[StorageEntry, ...] = (),
) -> Panel:
    """Build a renderable dashboard panel from collected metrics and storage."""
```

Replace the `Disk utilization` row with the loop, in the same position:

```python
    table.add_row("Swap", str(metrics["swap_summary"]))
    for entry in storage:
        table.add_row(entry.label, format_entry(entry))
    table.add_row("IPv4 address", str(metrics["ipv4_address"]))
```

`src/harbor_console/app.py` — inject the second collector:

```python
from harbor_console.storage import StorageEntry, collect_storage

StorageCollector = Callable[[], tuple[StorageEntry, ...]]
DashboardBuilder = Callable[[dict[str, str | float | int], tuple[StorageEntry, ...]], object]


def run(
    refresh_interval: float = 1.0,
    collector: MetricsCollector = collect_system_metrics,
    renderer: DashboardBuilder = build_dashboard,
    sleep: Callable[[float], None] = time.sleep,
    storage_collector: StorageCollector = collect_storage,
) -> int:
    """Run the Harbor Console refresh loop."""
    try:
        with Live(
            renderer(collector(), storage_collector()), refresh_per_second=4, screen=True
        ) as live:
            while True:
                sleep(refresh_interval)
                live.update(renderer(collector(), storage_collector()))
    except KeyboardInterrupt:
        return 0
```

`src/harbor_console/web.py` — delete the Disk row from `_host_table`:

```python
        ("Swap", snapshot.metrics["swap_summary"]),
        ("IPv4", snapshot.metrics["ipv4_address"]),
```

`src/harbor_console/webapp.py` — delete `"disk_utilization": 0.0,` from the degraded stub.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: See it run**

Run: `uv run harbor-console` and confirm one row per filesystem, then Ctrl+C. (On Windows the block and remote collectors degrade to nothing, which is itself the degradation path working; the full output is only visible on hpz440.)

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(console): one row per filesystem, and disk_utilization retires"
```

---

### Task 7: Deploy and verify on hpz440

**Files:** none — this is verification.

- [ ] **Step 1: Push and open the PR**

The branch already exists and carries the spec. Write the body to a scratch
file first -- a heredoc with this much prose in it has already broken once in
this project -- then:

```bash
git push origin feat/storage-reporting
gh pr create --title "Report the storage the host actually has" --body-file /tmp/pr-body.md
```

The body should name what the spec's ground-truth table showed, the two
deviations recorded at the top of this plan, and the fact that Copilot's four
findings on PR #10 are already folded into the spec being implemented.

- [ ] **Step 2: Confirm the real output before merging**

The plan's tests prove the shapes; only the host proves the shapes are right. Run against the deployed checkout after merge:

```bash
ssh gte@hpz440 "/opt/harbor-console/.venv/bin/python3 -c \"
from harbor_console.storage import collect_storage, format_entry
for e in collect_storage(): print(f'{e.label:24} {format_entry(e)}')
\""
```

Expected, from the ground truth in the spec: `/` at about 70% of 98 GiB, `/home/arm/media` around 2.4 TiB, `/boot` and `/boot/efi`, `VG ubuntu-vg 233.9 GiB unallocated`, the two `/mnt/nas/*` rows as `remote -- not measured`, and `8 image mounts (squashfs, read-only)`. No `sr0`, no `sda3`, no per-snap rows.

- [ ] **Step 3: Confirm both surfaces**

```bash
ssh gte@hpz440 "curl -s -k https://harbor.hpz440.ohr3023.org/ | grep -A12 '<h2>Storage'"
```

Expected: the same rows on the page. The tty1 panel is visible only at the console.
