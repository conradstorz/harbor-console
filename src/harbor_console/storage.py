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
    image_mounts_found = [p for p in found if getattr(p, "fstype", "") in IMAGE_FSTYPES]
    count = len(image_mounts_found)
    if not count:
        return []
    fstypes = sorted(set(getattr(p, "fstype", "") for p in image_mounts_found))
    fstype_str = ", ".join(fstypes)
    mount_word = "mount" if count == 1 else "mounts"
    return [
        StorageEntry(
            label="Image mounts",
            note=f"{count} image {mount_word} ({fstype_str}, read-only)",
        )
    ]


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
