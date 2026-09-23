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
