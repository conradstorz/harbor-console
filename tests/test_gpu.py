from harbor_console.gpu import NOTE_UNAVAILABLE, GpuEntry, format_gpu


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
