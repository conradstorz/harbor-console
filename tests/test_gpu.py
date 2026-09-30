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

    assert format_gpu(entry) == "busy 12% · 1.0/8 GiB · 54°C"


def test_format_keeps_a_fractional_vram_total():
    entry = GpuEntry(label="g", driver="amdgpu", vram_used=512 * 1024**2, vram_total=1536 * 1024**2)

    assert format_gpu(entry) == "0.5/1.5 GiB"


def test_format_fits_the_fullest_row_in_the_console_value_column():
    """80 columns minus the panel border, padding and this row's own label
    leaves 54 for the value; a GPU row that wraps eats one of the three rows
    of headroom the console has (ADR 20)."""
    entry = GpuEntry(
        label="GPU card1 (RTX 3060)",
        driver="nvidia",
        busy_percent=100,
        vram_used=int(11.9 * 1024**3),
        vram_total=12 * 1024**3,
        temp_c=100.0,
        power_w=170.0,
        power_limit_w=170.0,
        fan_percent=100,
    )

    assert len(format_gpu(entry)) <= 54


def test_format_appends_power_against_its_limit_then_fan():
    entry = GpuEntry(
        label="GPU card1 (RTX 3060)",
        driver="nvidia",
        busy_percent=100,
        vram_used=10734 * 1024**2,
        vram_total=12 * 1024**3,
        temp_c=83.0,
        power_w=167.38,
        power_limit_w=170.0,
        fan_percent=88,
    )

    assert format_gpu(entry) == (
        "busy 100% · 10.5/12 GiB · 83°C · 167/170 W · fan 88%"
    )


def test_format_shows_power_alone_when_the_limit_is_unknown():
    assert format_gpu(GpuEntry(label="g", driver="nvidia", power_w=12.4)) == "12 W"


def test_format_shows_nothing_for_a_limit_without_a_draw():
    assert format_gpu(GpuEntry(label="g", driver="nvidia", power_limit_w=170.0)) == (
        "no metrics exposed by nvidia"
    )


def test_format_shows_a_fan_alone():
    assert format_gpu(GpuEntry(label="g", driver="radeon", fan_percent=35)) == "fan 35%"


def test_format_shows_only_what_the_driver_exposed():
    entry = GpuEntry(label="GPU card0 (radeon)", driver="radeon", temp_c=35.0)

    assert format_gpu(entry) == "35°C"


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


def test_collect_reads_a_radeon_fan_as_a_percentage_of_pwm1_max(tmp_path):
    card(tmp_path, "card0", hwmon__hwmon2__pwm1="64\n", hwmon__hwmon2__pwm1_max="255\n")

    (entry,) = collect_gpus(tmp_path)

    assert entry.fan_percent == 25


def test_collect_assumes_the_usual_pwm_range_when_pwm1_max_is_missing(tmp_path):
    card(tmp_path, "card0", hwmon__hwmon2__pwm1="255\n")

    (entry,) = collect_gpus(tmp_path)

    assert entry.fan_percent == 100


def test_collect_reads_the_fan_from_the_same_hwmon_as_the_temperature(tmp_path):
    card(
        tmp_path,
        "card0",
        hwmon__hwmon10__temp1_input="11000",
        hwmon__hwmon10__pwm1="255",
        hwmon__hwmon2__temp1_input="55000",
        hwmon__hwmon2__pwm1="0",
    )

    (entry,) = collect_gpus(tmp_path)

    assert (entry.temp_c, entry.fan_percent) == (55.0, 0)


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


def test_collect_picks_the_lowest_numbered_hwmon_not_the_lexically_first(tmp_path):
    card(tmp_path, "card0", hwmon__hwmon10__temp1_input="11000", hwmon__hwmon2__temp1_input="55000")

    (entry,) = collect_gpus(tmp_path)

    assert entry.temp_c == 55.0


def test_collect_skips_a_hwmon_that_has_neither_temperature_nor_fan(tmp_path):
    card(
        tmp_path,
        "card0",
        hwmon__hwmon2__name="other",
        hwmon__hwmon10__temp1_input="41000",
        hwmon__hwmon10__pwm1="128",
    )

    (entry,) = collect_gpus(tmp_path)

    assert (entry.temp_c, entry.fan_percent) == (41.0, 50)


def test_collect_takes_a_hwmon_with_only_a_fan_when_no_hwmon_has_a_temperature(tmp_path):
    card(tmp_path, "card0", hwmon__hwmon2__name="other", hwmon__hwmon3__pwm1="255")

    (entry,) = collect_gpus(tmp_path)

    assert (entry.temp_c, entry.fan_percent) == (None, 100)


def test_collect_keeps_a_card_whose_device_directory_is_missing(tmp_path):
    (tmp_path / "card0").mkdir()

    assert collect_gpus(tmp_path) == (GpuEntry(label="GPU card0"),)


def test_collect_ignores_a_hwmon_that_is_a_file(tmp_path):
    card(tmp_path, "card0")
    (tmp_path / "card0" / "device" / "hwmon").write_text("x")

    (entry,) = collect_gpus(tmp_path)

    assert entry.temp_c is None
    assert entry.driver == "radeon"


def test_collect_reports_unavailable_when_the_root_cannot_be_listed(tmp_path):
    assert collect_gpus(tmp_path / "missing") == (GpuEntry(label="GPU", note=NOTE_UNAVAILABLE),)


def test_collect_returns_nothing_for_a_host_with_no_cards(tmp_path):
    (tmp_path / "version").write_text("drm 1.1.0\n")

    assert collect_gpus(tmp_path) == ()


def test_collect_defaults_to_the_real_sysfs_root():
    import inspect

    assert inspect.signature(collect_gpus).parameters["drm_root"].default == "/sys/class/drm"


# --- NVIDIA: nothing in sysfs, everything through NVML ---


def nvidia_card(root: Path, name: str, bus_id: str | None = "0000:02:00.0") -> Path:
    device = card(root, name, driver=None)
    lines = ["DRIVER=nvidia", "PCI_ID=10DE:2503"]
    if bus_id is not None:
        lines.append(f"PCI_SLOT_NAME={bus_id}")
    (device / "uevent").write_text("\n".join(lines) + "\n")
    return device


class FakeNvmlMetrics:
    def __init__(self, metrics=None):
        self.metrics = metrics
        self.asked: list[str] = []

    def __call__(self, bus_id: str):
        self.asked.append(bus_id)
        return self.metrics


def test_collect_fills_an_nvidia_card_from_nvml_by_bus_id(tmp_path):
    from harbor_console.nvml import NvmlMetrics

    nvidia_card(tmp_path, "card1", bus_id="0000:02:00.0")
    nvml = FakeNvmlMetrics(
        NvmlMetrics(
            name="NVIDIA GeForce RTX 3060",
            busy_percent=7,
            vram_used=5 * 1024**3,
            vram_total=12 * 1024**3,
            temp_c=48.0,
            power_w=30.2,
            power_limit_w=170.0,
            fan_percent=0,
        )
    )

    (entry,) = collect_gpus(tmp_path, nvml=nvml)

    assert nvml.asked == ["0000:02:00.0"]
    assert entry == GpuEntry(
        label="GPU card1 (RTX 3060)",
        driver="nvidia",
        busy_percent=7,
        vram_used=5 * 1024**3,
        vram_total=12 * 1024**3,
        temp_c=48.0,
        power_w=30.2,
        power_limit_w=170.0,
        fan_percent=0,
    )
    assert format_gpu(entry) == "busy 7% · 5.0/12 GiB · 48°C · 30/170 W · fan 0%"


def test_collect_labels_an_nvidia_card_by_its_short_name(tmp_path):
    from harbor_console.nvml import NvmlMetrics

    nvidia_card(tmp_path, "card1")

    (entry,) = collect_gpus(tmp_path, nvml=FakeNvmlMetrics(NvmlMetrics(name="NVIDIA GeForce RTX 3060")))
    assert entry.label == "GPU card1 (RTX 3060)"

    (entry,) = collect_gpus(tmp_path, nvml=FakeNvmlMetrics(NvmlMetrics(name="Quadro P400")))
    assert entry.label == "GPU card1 (Quadro P400)"

    (entry,) = collect_gpus(tmp_path, nvml=FakeNvmlMetrics(NvmlMetrics(name="")))
    assert entry.label == "GPU card1 (nvidia)"


def test_collect_does_not_ask_nvml_about_other_drivers(tmp_path):
    card(tmp_path, "card0", hwmon__hwmon2__temp1_input="35000\n")
    nvml = FakeNvmlMetrics()

    collect_gpus(tmp_path, nvml=nvml)

    assert nvml.asked == []


def test_collect_leaves_an_nvidia_card_bare_when_nvml_has_nothing(tmp_path):
    nvidia_card(tmp_path, "card1")

    (entry,) = collect_gpus(tmp_path, nvml=FakeNvmlMetrics(None))

    assert entry == GpuEntry(label="GPU card1 (nvidia)", driver="nvidia")
    assert format_gpu(entry) == "no metrics exposed by nvidia"


def test_collect_does_not_ask_nvml_without_a_bus_id(tmp_path):
    nvidia_card(tmp_path, "card1", bus_id=None)
    nvml = FakeNvmlMetrics()

    (entry,) = collect_gpus(tmp_path, nvml=nvml)

    assert nvml.asked == []
    assert entry.driver == "nvidia"


def test_collect_defaults_nvml_to_the_real_query():
    import inspect

    from harbor_console.nvml import nvml_metrics

    assert inspect.signature(collect_gpus).parameters["nvml"].default is nvml_metrics
