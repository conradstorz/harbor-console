from types import SimpleNamespace

from harbor_console.storage import (
    NOTE_REMOTE,
    NOTE_UNAVAILABLE,
    StorageEntry,
    format_entry,
    image_mounts,
    local_filesystems,
    remote_mounts,
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


def test_image_mounts_singular_wording_for_one_mount():
    """1 mount uses singular 'mount', not plural 'mounts'."""
    partitions = lambda all=False: [
        part("/dev/mapper/vg-root", "/", "ext4"),
        part("/dev/loop0", "/snap/core20/2866", "squashfs", opts="ro"),
    ]

    entries = image_mounts(partitions=partitions)

    assert len(entries) == 1
    assert format_entry(entries[0]) == "1 image mount (squashfs, read-only)"


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
    assert format_entry(entries[0]) == "9 image mounts (erofs, squashfs, read-only)"


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
