from pathlib import Path

from harbor_console.gpu import NOTE_UNAVAILABLE, GpuEntry, format_gpu, collect_gpus


def test_format_joins_every_present_field_in_order():
    entry = GpuEntry(
        label="GPU card0 (amdgpu)",
        driver="amdgpu",
        busy_percent=12,
        vram_used=1 * 1024**3,
        vram_total=8 * 1024**3,
        temp_c=54.0,
    )

    assert format_gpu(entry) == "busy 12% · VRAM 1.0 / 8.0 GiB (12.5%) · 54 °C"


def test_format_shows_only_what_the_driver_exposed():
    entry = GpuEntry(label="GPU card0 (radeon)", driver="radeon", temp_c=35.0)

    assert format_gpu(entry) == "35 °C"


def test_format_omits_vram_when_only_half_of_the_pair_is_known():
    entry = GpuEntry(label="GPU card0 (amdgpu)", driver="amdgpu", vram_used=5, busy_percent=3)

    assert format_gpu(entry) == "busy 3%"


def test_format_omits_vram_when_total_is_zero():
    entry = GpuEntry(label="GPU card0 (amdgpu)", driver="amdgpu", vram_used=0, vram_total=0)

    assert format_gpu(entry) == "no metrics exposed by amdgpu"


def test_format_names_the_driver_when_nothing_was_exposed():
    assert format_gpu(GpuEntry(label="GPU card0 (radeon)", driver="radeon")) == (
        "no metrics exposed by radeon"
    )


def test_format_with_no_driver_and_nothing_exposed():
    assert format_gpu(GpuEntry(label="GPU card0")) == "no metrics exposed"


def test_format_renders_a_note_alone():
    assert format_gpu(GpuEntry(label="GPU", note=NOTE_UNAVAILABLE)) == "unavailable"


def card(root: Path, name: str, driver: str | None = "radeon", **files: str) -> Path:
    """Build `<root>/<name>/device/...` the way sysfs lays it out.

    `files` are written relative to `device/`; a key with `__` in it is a
    nested path (`hwmon__hwmon2__temp1_input`). `driver=None` writes no
    `uevent` at all.
    """
    device = root / name / "device"
    device.mkdir(parents=True)
    if driver is not None:
        (device / "uevent").write_text(f"DRIVER={driver}\nPCI_ID=1002:6610\n")
    for key, value in files.items():
        target = device.joinpath(*key.split("__"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)
    return device


def test_collect_reads_a_radeon_card_with_only_a_temperature(tmp_path):
    card(tmp_path, "card0", hwmon__hwmon2__temp1_input="35000\n")

    (entry,) = collect_gpus(tmp_path)

    assert entry == GpuEntry(label="GPU card0 (radeon)", driver="radeon", temp_c=35.0)


def test_collect_reads_every_amdgpu_metric(tmp_path):
    card(
        tmp_path,
        "card0",
        driver="amdgpu",
        gpu_busy_percent="12\n",
        mem_info_vram_used=str(1024**3),
        mem_info_vram_total=str(8 * 1024**3),
        hwmon__hwmon0__temp1_input="54000",
    )

    (entry,) = collect_gpus(tmp_path)

    assert entry == GpuEntry(
        label="GPU card0 (amdgpu)",
        driver="amdgpu",
        busy_percent=12,
        vram_used=1024**3,
        vram_total=8 * 1024**3,
        temp_c=54.0,
    )


def test_collect_ignores_connectors_render_nodes_and_files(tmp_path):
    card(tmp_path, "card0")
    (tmp_path / "card0-DP-1").mkdir()
    (tmp_path / "card0-DVI-I-1").mkdir()
    (tmp_path / "renderD128").mkdir()
    (tmp_path / "version").write_text("drm 1.1.0 20060810\n")

    entries = collect_gpus(tmp_path)

    assert [e.label for e in entries] == ["GPU card0 (radeon)"]


def test_collect_sorts_cards_by_number(tmp_path):
    card(tmp_path, "card10", driver="amdgpu")
    card(tmp_path, "card2", driver="nouveau")
    card(tmp_path, "card0")

    assert [e.label for e in collect_gpus(tmp_path)] == [
        "GPU card0 (radeon)",
        "GPU card2 (nouveau)",
        "GPU card10 (amdgpu)",
    ]


def test_collect_keeps_a_card_whose_driver_exposes_nothing(tmp_path):
    card(tmp_path, "card0")

    (entry,) = collect_gpus(tmp_path)

    assert entry == GpuEntry(label="GPU card0 (radeon)", driver="radeon")
    assert format_gpu(entry) == "no metrics exposed by radeon"


def test_collect_labels_a_card_bare_when_uevent_is_missing(tmp_path):
    card(tmp_path, "card0", driver=None)

    (entry,) = collect_gpus(tmp_path)

    assert entry == GpuEntry(label="GPU card0")


def test_collect_labels_a_card_bare_when_uevent_has_no_driver_line(tmp_path):
    device = card(tmp_path, "card0", driver=None)
    (device / "uevent").write_text("PCI_ID=1002:6610\n")

    (entry,) = collect_gpus(tmp_path)

    assert entry.label == "GPU card0"
    assert entry.driver == ""


def test_collect_drops_only_the_field_that_is_malformed(tmp_path):
    card(
        tmp_path,
        "card0",
        driver="amdgpu",
        gpu_busy_percent="garbage",
        hwmon__hwmon0__temp1_input="41000",
    )

    (entry,) = collect_gpus(tmp_path)

    assert entry.busy_percent is None
    assert entry.temp_c == 41.0


def test_collect_reports_unavailable_when_the_root_cannot_be_listed(tmp_path):
    assert collect_gpus(tmp_path / "missing") == (GpuEntry(label="GPU", note=NOTE_UNAVAILABLE),)


def test_collect_returns_nothing_for_a_host_with_no_cards(tmp_path):
    (tmp_path / "version").write_text("drm 1.1.0\n")

    assert collect_gpus(tmp_path) == ()


def test_collect_defaults_to_the_real_sysfs_root():
    import inspect

    assert inspect.signature(collect_gpus).parameters["drm_root"].default == "/sys/class/drm"
