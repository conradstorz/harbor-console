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
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import psutil

from harbor_console.system import format_bytes, format_usage

#: Never measured -- see the module docstring. `remote_mounts` names them from
#: /proc/mounts instead, and `local_filesystems` drops them before any
#: `disk_usage` call. psutil's `all=False` already omits them on Linux (they are
#: nodev filesystems), but the promise that nothing here blocks on a dead server
#: is this module's own rather than a dependency's implementation detail.
REMOTE_FSTYPES = frozenset({"cifs", "smb3", "smbfs", "nfs", "nfs4", "fuse.sshfs"})

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
NOTE_PERMISSION_DENIED = "permission denied"


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
    sizes: Mapping[str, int] | None = None,
) -> list[StorageEntry]:
    """Every mounted local filesystem, root first, then by mountpoint.

    Each `usage` call is guarded on its own: one unreadable mount becomes
    `unavailable` and the rows around it survive. Failing to enumerate at all
    yields one `unavailable` entry rather than an empty list -- nothing found
    and nothing looked at must not render alike (ADR 18).

    Either failure branch consults `sizes` -- typically `mounted_device_sizes()`
    -- for a size-only render when it has the mountpoint (the size-only render
    shape); otherwise the entry stays note-only. Only the note text differs:
    `usage()` raising `PermissionError` gets `NOTE_PERMISSION_DENIED` rather
    than `NOTE_UNAVAILABLE`, because it is the specific, expected case here --
    the unprivileged `harbor` user on `/home/arm/media` -- and a mount
    `os.statvfs` cannot read for this user often still has a size `lsblk`
    already knows. Any other failure still gets `NOTE_UNAVAILABLE`, with a
    size too if `sizes` has one.
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
        except PermissionError:
            entries.append(
                StorageEntry(
                    label=mountpoint,
                    total=(sizes.get(mountpoint) if sizes else None),
                    note=NOTE_PERMISSION_DENIED,
                )
            )
        except Exception:
            entries.append(
                StorageEntry(
                    label=mountpoint,
                    total=(sizes.get(mountpoint) if sizes else None),
                    note=NOTE_UNAVAILABLE,
                )
            )
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
            label=f"{count} image {mount_word}",
            note=f"({fstype_str}, read-only)",
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


#: A single, unkeyed 60-second cache for the last successful `_lsblk_nodes`
#: read. Not keyed on `run` or `timeout` -- this project runs one host, one
#: `lsblk` invocation shape, and block topology does not change at 1 Hz. A
#: failed read is never stored here -- see `_lsblk_nodes`. Tests that fake
#: `lsblk` and need a fresh read must call `_clear_lsblk_cache()` first; see
#: `_lsblk_nodes`.
_LSBLK_CACHE_TTL_SECONDS = 60.0
_lsblk_cache: dict[str, object] = {"result": None, "timestamp": None}


def _clear_lsblk_cache() -> None:
    """Reset the lsblk read cache.

    Tests that fake `lsblk` must call this before a call whose result should
    reflect that fake rather than an earlier cached read -- the cache is not
    keyed on `run` identity, so without a reset a stale result from an
    earlier call is returned as-is, even one made with a different `run`.
    """
    _lsblk_cache["result"] = None
    _lsblk_cache["timestamp"] = None


def _read_lsblk_nodes(run: Callable[..., object], timeout: float) -> list[dict] | None:
    """One uncached `lsblk` read, flattened. `None` on any failure to read it."""
    try:
        completed = run(
            ["lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(completed, "returncode", 1) != 0:
        return None
    try:
        tree = json.loads(getattr(completed, "stdout", "") or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(tree, dict) or not isinstance(tree.get("blockdevices"), list):
        return None
    return _walk(tree["blockdevices"])


def _lsblk_nodes(run: Callable[..., object], timeout: float) -> list[dict] | None:
    """The flat, walked `lsblk` block-device tree, cached for 60 seconds.

    Returns `None` when `lsblk` could not be read at all -- missing binary,
    non-zero exit, timeout, or output that is not the expected JSON shape
    (see `_read_lsblk_nodes`). Only a successful read is cached; a failed one
    is not stored and does not start the TTL, so a transient `lsblk` failure
    costs at most the one call it happened on -- the very next call retries
    for real rather than replaying the failure for the rest of the window.
    Both `block_devices` and `mounted_device_sizes` read through here rather
    than duplicating the subprocess call, so they share one cached read per
    TTL window.

    The cache is a single module-level slot, not keyed on `run` or `timeout`
    -- see `_clear_lsblk_cache`. Tests that fake `lsblk` must reset it first
    if they need this call to actually invoke their fake after a prior
    successful read.
    """
    now = time.monotonic()
    timestamp = _lsblk_cache["timestamp"]
    if timestamp is not None and now - timestamp < _LSBLK_CACHE_TTL_SECONDS:  # type: ignore[operator]
        return _lsblk_cache["result"]  # type: ignore[return-value]

    result = _read_lsblk_nodes(run, timeout)
    if result is not None:
        _lsblk_cache["result"] = result
        _lsblk_cache["timestamp"] = now
    return result


def mounted_device_sizes(
    run: Callable[..., object] = subprocess.run,
    timeout: float = LSBLK_TIMEOUT_SECONDS,
) -> dict[str, int]:
    """Mountpoint -> device size in bytes, from the same cached `lsblk` read.

    A node can have multiple mountpoints; every non-null one maps to that
    node's size. `{}` when `lsblk` could not be read, or when nothing in the
    tree has both a mountpoint and a known size. This is how
    `local_filesystems` can still report a size for a mount `disk_usage`
    cannot read unprivileged: `lsblk` already knows the device's size.

    That size is the *device or logical volume*'s size, not the filesystem's
    -- `lsblk`'s view, not `statvfs`'s. A size-only row this feeds (e.g.
    `/home/arm/media`) therefore sits a few percent above what `disk_usage`
    would report for that same mount, and on a different basis than every
    measured row in the table: device/LV size versus filesystem-reported
    size, which nets out overhead like the ext4 journal and reserved blocks.
    The `permission denied` note is currently the only hint that a row is
    different in kind this way.
    """
    nodes = _lsblk_nodes(run, timeout)
    if nodes is None:
        return {}

    sizes: dict[str, int] = {}
    for node in nodes:
        size = node.get("size")
        if not isinstance(size, int):
            continue
        for mountpoint in node.get("mountpoints") or []:
            if mountpoint:
                sizes[mountpoint] = size
    return sizes


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

    A missing or failing `lsblk` -- missing binary, non-zero exit, a timeout,
    or output that is not the JSON shape expected -- yields one
    `unavailable` sentinel entry rather than an empty list, the same rule
    `local_filesystems` and `remote_mounts` already follow: nothing found and
    nothing looked at must not render alike (ADR 18). `lsblk` running fine
    and simply reporting nothing relevant still yields `[]`.

    `run` and `timeout` are silently inert on a cache hit: `_lsblk_nodes`
    reads through a shared 60-second cache (see `_clear_lsblk_cache`), and on
    a hit `_read_lsblk_nodes` is never called, so an injected fake `run` or a
    custom `timeout` passed here can be ignored without warning and the
    caller gets another caller's cached read instead. A test that fakes
    `run` and finds it was never invoked is hitting exactly this.
    """
    nodes = _lsblk_nodes(run, timeout)
    if nodes is None:
        return [StorageEntry(label="block devices", note=NOTE_UNAVAILABLE)]

    slack: list[StorageEntry] = []
    stray: list[StorageEntry] = []
    for node in nodes:
        children = [c for c in (node.get("children") or []) if isinstance(c, dict)]
        fstype = node.get("fstype") or ""
        size = node.get("size")
        name = node.get("name") or ""
        if not isinstance(size, int):
            continue
        if fstype == "LVM2_member":
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


def collect_storage(
    filesystems: Callable[[], list[StorageEntry]] = lambda: local_filesystems(
        sizes=mounted_device_sizes()
    ),
    blocks: Callable[[], list[StorageEntry]] = block_devices,
    remote: Callable[[], list[StorageEntry]] = remote_mounts,
    images: Callable[[], list[StorageEntry]] = image_mounts,
) -> tuple[StorageEntry, ...]:
    """Every storage entry this host has, in display order.

    Measured filesystems first, because they are what anyone came to read; then
    the layer underneath them, then what is merely named, then the collapsed
    image-mount line last. `filesystems`' default sources its size-only
    fallback from `mounted_device_sizes()`, so a mount `disk_usage` cannot
    read unprivileged can still report the size `lsblk` already knows.
    """
    return (*filesystems(), *blocks(), *remote(), *images())
