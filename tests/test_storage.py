import json
import subprocess
from types import SimpleNamespace

import pytest

import harbor_console.storage as storage
from harbor_console.storage import (
    LSBLK_TIMEOUT_SECONDS,
    NOTE_NO_FILESYSTEM,
    NOTE_PERMISSION_DENIED,
    NOTE_REMOTE,
    NOTE_UNALLOCATED,
    NOTE_UNAVAILABLE,
    StorageEntry,
    _clear_lsblk_cache,
    block_devices,
    collect_storage,
    format_entry,
    image_mounts,
    local_filesystems,
    mounted_device_sizes,
    remote_mounts,
)


@pytest.fixture(autouse=True)
def _reset_lsblk_cache():
    """Every test starts with an empty lsblk cache.

    The cache is a single, unkeyed module-level slot (see
    `storage._clear_lsblk_cache`'s docstring) -- without this, a `block_devices`
    or `mounted_device_sizes` call in one test could silently reuse a fake
    `lsblk` result cached by an earlier test in this file, since pytest runs a
    file's tests in source order. Tests that specifically exercise caching
    behavior make more than one call within their own body and reset (or not)
    exactly where the test cares.
    """
    _clear_lsblk_cache()
    yield


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
    """A PermissionError with no known size is note-only, but with its own note."""
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
    assert entries[1].note == NOTE_PERMISSION_DENIED
    assert entries[1].total is None


def test_local_filesystems_permission_denied_with_known_size_is_size_only():
    """The motivating case: /home/arm/media, unreadable unprivileged, sized by lsblk."""
    partitions = lambda all=False: [part("/dev/mapper/vg-media", "/home/arm/media", "ext4")]

    def denied(_mp):
        raise PermissionError("Permission denied: '/home/arm/media'")

    entries = local_filesystems(
        partitions=partitions,
        usage=denied,
        sizes={"/home/arm/media": 2638829584384},
    )

    assert len(entries) == 1
    entry = entries[0]
    assert entry.label == "/home/arm/media"
    assert entry.total == 2638829584384
    assert entry.used is None
    assert entry.percent is None
    assert entry.note == NOTE_PERMISSION_DENIED
    assert format_entry(entry) == "2457.6 GiB permission denied"


def test_local_filesystems_permission_denied_without_known_size_is_note_only():
    partitions = lambda all=False: [part("/dev/mapper/vg-media", "/home/arm/media", "ext4")]

    def denied(_mp):
        raise PermissionError("nope")

    entries = local_filesystems(
        partitions=partitions,
        usage=denied,
        sizes={"/some/other/mount": 123},
    )

    assert len(entries) == 1
    assert entries[0].total is None
    assert entries[0].note == NOTE_PERMISSION_DENIED
    assert format_entry(entries[0]) == NOTE_PERMISSION_DENIED


def test_local_filesystems_non_permission_failure_with_known_size_stays_unavailable():
    """A known size does not mask a real (non-permission) failure as healthy."""
    partitions = lambda all=False: [part("/dev/mapper/vg-media", "/home/arm/media", "ext4")]

    def broken(_mp):
        raise OSError("stale NFS handle")

    entries = local_filesystems(
        partitions=partitions,
        usage=broken,
        sizes={"/home/arm/media": 2638829584384},
    )

    assert len(entries) == 1
    entry = entries[0]
    assert entry.total == 2638829584384
    assert entry.note == NOTE_UNAVAILABLE


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
    assert entries[0].label == "2 image mounts"
    assert entries[0].note == "(squashfs, read-only)"
    assert format_entry(entries[0]) == "(squashfs, read-only)"


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


def test_image_mounts_singular_wording_for_one_mount():
    """1 mount uses singular 'mount', not plural 'mounts'."""
    partitions = lambda all=False: [
        part("/dev/mapper/vg-root", "/", "ext4"),
        part("/dev/loop0", "/snap/core20/2866", "squashfs", opts="ro"),
    ]

    entries = image_mounts(partitions=partitions)

    assert len(entries) == 1
    assert entries[0].label == "1 image mount"
    assert entries[0].note == "(squashfs, read-only)"


def test_image_mounts_mixed_fstypes_sorted_alphabetically():
    """Multiple different fstypes are listed sorted and comma-separated."""
    partitions = lambda all=False: [
        part("/dev/mapper/vg-root", "/", "ext4"),
        part("/dev/loop0", "/snap/core20/2866", "squashfs", opts="ro"),
        part("/dev/loop1", "/snap/lxd/40575", "squashfs", opts="ro"),
        part("/dev/loop2", "/snap/ubuntu-core/13486", "squashfs", opts="ro"),
        part("/dev/loop3", "/snap/other/1", "squashfs", opts="ro"),
        part("/dev/loop4", "/snap/other/2", "squashfs", opts="ro"),
        part("/dev/loop5", "/snap/other/3", "squashfs", opts="ro"),
        part("/dev/loop6", "/snap/other/4", "squashfs", opts="ro"),
        part("/dev/loop7", "/snap/other/5", "squashfs", opts="ro"),
        part("/dev/loop8", "/sys/kernel/security", "erofs", opts="ro"),
    ]

    entries = image_mounts(partitions=partitions)

    assert len(entries) == 1
    assert entries[0].label == "9 image mounts"
    assert entries[0].note == "(erofs, squashfs, read-only)"


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


def test_smbfs_mounts_are_named_by_remote_and_never_measured_by_local(tmp_path):
    """smbfs is a remote fstype alongside cifs/smb3/nfs/nfs4/fuse.sshfs."""
    path = tmp_path / "mounts"
    path.write_text("//nas/share /mnt/smb smbfs rw 0 0\n")

    entries = remote_mounts(mounts_path=str(path))

    assert [e.label for e in entries] == ["/mnt/smb"]
    assert entries[0].note == NOTE_REMOTE

    partitions = lambda all=False: [
        part("/dev/mapper/vg-root", "/", "ext4"),
        part("//nas/share", "/mnt/smb", "smbfs"),
    ]
    measured = []

    def recording(mountpoint):
        measured.append(mountpoint)
        return usage(65 * GIB, 98 * GIB, 70.0)

    fs_entries = local_filesystems(partitions=partitions, usage=recording)

    assert measured == ["/"]
    assert [e.label for e in fs_entries] == ["/"]


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


def test_block_devices_reports_full_slack_for_pv_with_no_logical_volumes():
    """A PV given to LVM but with no LVs yet is entirely unallocated slack."""
    tree = {
        "blockdevices": [
            {
                "name": "sda3",
                "type": "part",
                "size": 2997315698688,
                "fstype": "LVM2_member",
                "mountpoints": [None],
                "children": [],
            }
        ]
    }

    entries = block_devices(run=fake_lsblk(tree))

    assert len(entries) == 1
    entry = entries[0]
    assert entry.note == NOTE_UNALLOCATED
    assert entry.total == 2997315698688
    # No child to derive a VG name from, so the label falls back to the raw device name.
    assert entry.label == "sda3"


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


_BLOCK_DEVICES_UNAVAILABLE = [StorageEntry(label="block devices", note=NOTE_UNAVAILABLE)]


def test_block_devices_degrades_on_timeout():
    run = fake_lsblk(raises=subprocess.TimeoutExpired(cmd="lsblk", timeout=5.0))

    assert block_devices(run=run) == _BLOCK_DEVICES_UNAVAILABLE


def test_block_devices_degrades_when_lsblk_is_missing():
    run = fake_lsblk(raises=FileNotFoundError("lsblk"))

    assert block_devices(run=run) == _BLOCK_DEVICES_UNAVAILABLE


def test_block_devices_degrades_on_garbage_output():
    assert block_devices(run=fake_lsblk(stdout="not json at all")) == _BLOCK_DEVICES_UNAVAILABLE


def test_block_devices_degrades_on_nonzero_exit():
    assert block_devices(run=fake_lsblk(LSBLK_TREE, returncode=1)) == _BLOCK_DEVICES_UNAVAILABLE


def test_block_devices_stays_empty_when_lsblk_runs_fine_but_finds_nothing():
    """lsblk succeeding with no relevant entries is [] -- not the sentinel.

    The sentinel means "could not look"; this is "looked, nothing there."
    """
    entries = block_devices(run=fake_lsblk({"blockdevices": []}))

    assert entries == []


def test_mounted_device_sizes_maps_every_mountpoint_to_its_devices_size():
    sizes = mounted_device_sizes(run=fake_lsblk(LSBLK_TREE))

    assert sizes["/home/arm/media"] == 2638829584384
    assert sizes["/"] == 107374182400
    assert sizes["/boot"] == 2147483648
    assert sizes["/boot/efi"] == 1127219200
    # A node whose only mountpoint is null contributes nothing.
    assert "sda" not in sizes


def test_mounted_device_sizes_empty_when_lsblk_times_out():
    run = fake_lsblk(raises=subprocess.TimeoutExpired(cmd="lsblk", timeout=5.0))

    assert mounted_device_sizes(run=run) == {}


def test_mounted_device_sizes_empty_when_lsblk_is_missing():
    run = fake_lsblk(raises=FileNotFoundError("lsblk"))

    assert mounted_device_sizes(run=run) == {}


def test_mounted_device_sizes_empty_on_garbage_output():
    assert mounted_device_sizes(run=fake_lsblk(stdout="not json at all")) == {}


def test_lsblk_cache_returns_stale_result_without_a_reset():
    """Within the TTL, a second call reuses the first read -- even with a
    different `run` -- because the cache is not keyed on `run` identity."""
    run_a = fake_lsblk(LSBLK_TREE)
    first = block_devices(run=run_a)

    run_b = fake_lsblk({"blockdevices": []})
    second = block_devices(run=run_b)

    assert second == first
    assert second != []
    assert run_b.calls == []  # run_b's lsblk was never actually invoked


def test_lsblk_cache_reset_returns_a_fresh_result():
    run_a = fake_lsblk(LSBLK_TREE)
    block_devices(run=run_a)

    _clear_lsblk_cache()

    run_b = fake_lsblk({"blockdevices": []})
    second = block_devices(run=run_b)

    assert second == []
    assert len(run_b.calls) == 1


def test_lsblk_failed_read_is_not_cached():
    """A transient lsblk failure must not poison the cache for the full TTL --
    the next call (even immediately after, no _clear_lsblk_cache) should retry
    lsblk for real rather than replaying the cached failure."""
    run_a = fake_lsblk(raises=FileNotFoundError("lsblk"))
    first = block_devices(run=run_a)
    assert first == _BLOCK_DEVICES_UNAVAILABLE

    run_b = fake_lsblk(LSBLK_TREE)
    second = block_devices(run=run_b)

    assert second != _BLOCK_DEVICES_UNAVAILABLE
    assert len(run_b.calls) == 1  # run_b's lsblk actually ran -- not a cached failure


def test_mounted_device_sizes_shares_the_block_devices_cache():
    """Both readers go through the same cached `_lsblk_nodes` read."""
    run_a = fake_lsblk(LSBLK_TREE)
    block_devices(run=run_a)

    run_b = fake_lsblk({"blockdevices": []})
    sizes = mounted_device_sizes(run=run_b)

    assert sizes["/home/arm/media"] == 2638829584384
    assert run_b.calls == []


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


def test_collect_storage_names_a_remote_mount_exactly_once(tmp_path):
    """A CIFS mount must come through `collect_storage` named, not measured,
    and not both -- the design document's promise that combines what
    `test_local_filesystems_never_measures_a_remote_mount` and
    `test_remote_mounts_names_network_mounts_and_nothing_else` each prove on
    their own half of the pipeline.
    """
    partitions = lambda all=False: [
        part("/dev/mapper/vg-root", "/", "ext4"),
        part("//nas/photo", "/mnt/nas/photos", "cifs"),
    ]
    mounts_path = tmp_path / "mounts"
    mounts_path.write_text("//nas/photo /mnt/nas/photos cifs rw,relatime,vers=3.1.1 0 0\n")

    entries = collect_storage(
        filesystems=lambda: local_filesystems(partitions=partitions),
        remote=lambda: remote_mounts(mounts_path=str(mounts_path)),
        blocks=lambda: [],
        images=lambda: [],
    )

    matches = [e for e in entries if e.label == "/mnt/nas/photos"]
    assert len(matches) == 1
    assert matches[0].note == NOTE_REMOTE


def test_collect_storage_defaults_touch_the_real_host_without_raising():
    """The release criterion: a collector never raises on a hostile environment.

    Deliberately exercises the real machine -- do not rewrite this to use
    fakes; that is `test_collect_storage_names_a_remote_mount_exactly_once`'s
    job.
    """
    assert isinstance(collect_storage(), tuple)


def test_collect_storage_default_filesystems_wiring_passes_lsblk_sizes(monkeypatch):
    """Pins collect_storage's default filesystems= lambda to actually forward
    mounted_device_sizes() into local_filesystems(sizes=...). Every other test
    in this file injects sizes= by hand, so this is the only test that would
    catch that argument silently dropping from the default wiring -- the exact
    defect the permission-denied-with-known-size fix was for."""
    known_sizes = {"/home/arm/media": 2638829584384}
    monkeypatch.setattr(storage, "mounted_device_sizes", lambda *a, **kw: known_sizes)

    captured = {}

    def spy_local_filesystems(*, sizes=None, **kwargs):
        captured["sizes"] = sizes
        return []

    monkeypatch.setattr(storage, "local_filesystems", spy_local_filesystems)

    storage.collect_storage(blocks=lambda: [], remote=lambda: [], images=lambda: [])

    assert captured["sizes"] == known_sizes
