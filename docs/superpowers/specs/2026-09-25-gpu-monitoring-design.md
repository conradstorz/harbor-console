# GPU monitoring: one row per card, from sysfs

Date: 2026-09-25
Status: implemented 2026-09-25

## The problem

Neither surface says anything about the GPUs in the host. hpz440 carries one
today -- an AMD Radeon (Oland, PCI `1002:6610`) on the legacy `radeon` kernel
driver -- and more may follow. No vendor tool is installed: there is no
`nvidia-smi` and no `rocm-smi`, and the `radeon` driver exposes no
utilization or VRAM counters. What sysfs does expose, at design time:

| Path under `/sys/class/drm/card0/device` | Value | Exposed by |
|---|---|---|
| `uevent` (`DRIVER=radeon` line) | `radeon` | every PCI device |
| `vendor`, `device` | `0x1002`, `0x6610` | every PCI device |
| `hwmon/hwmon2/temp1_input` | `35000` (millidegrees) | `radeon`, `amdgpu`, `nouveau`, `i915` on some parts |
| `gpu_busy_percent` | absent | `amdgpu` only |
| `mem_info_vram_used`, `mem_info_vram_total` | absent | `amdgpu` only |

The `radeon` driver is the worst case: one number. An `amdgpu` or `nouveau`
card exposes all five. The proprietary NVIDIA driver exposes none of them
through DRM and needs `nvidia-smi`, which is out of scope here.

## Decision

A new collector, `gpu.py`, shaped like `storage.py`: it reads sysfs and
returns one entry per card, every metric optional, and both surfaces render
one row per entry. It reads files; it runs no subprocess, so nothing here can
hang and nothing needs a timeout.

### Collector: `src/harbor_console/gpu.py`

```python
@dataclass(frozen=True)
class GpuEntry:
    label: str                    # "GPU card0 (radeon)", or "GPU" for the sentinel
    driver: str = ""              # basename of device/driver, "" when unreadable
    busy_percent: int | None = None
    vram_used: int | None = None  # bytes
    vram_total: int | None = None # bytes
    temp_c: float | None = None
    note: str = ""

NOTE_UNAVAILABLE = "unavailable"
NOTE_NO_METRICS = "no metrics exposed by {driver}"

def collect_gpus(drm_root: str | Path = "/sys/class/drm") -> tuple[GpuEntry, ...]: ...
def format_gpu(entry: GpuEntry) -> str: ...
```

`collect_gpus`:

- Lists `drm_root`. Entries whose name matches `^card\d+$` are cards;
  `card0-DP-1` and the other connector nodes, `renderD128` and `version` are
  not. Cards sort by number.
- Per card, each read is guarded on its own and yields `None` when the file
  is missing, unreadable or not a number: the `DRIVER=` line of
  `device/uevent` (a regular file, read in preference to the `device/driver`
  symlink so a test tree needs no symlinks), `device/gpu_busy_percent` (int), `device/mem_info_vram_used`
  and `device/mem_info_vram_total` (int bytes), and the first
  `device/hwmon/hwmon*/temp1_input` found (int millidegrees, divided by
  1000). One bad file never costs the card its row or the other cards
  theirs.
- Label is `GPU card<N> (<driver>)`, or `GPU card<N>` when `uevent` has no
  `DRIVER=` line or cannot be read.
- `drm_root` cannot be listed: return one entry, `GpuEntry(label="GPU",
  note=NOTE_UNAVAILABLE)`. Nothing found and nothing looked at must not
  render alike (ADR 18).
- No cards: return an empty tuple. The renderers own the "none detected"
  wording, so the collector reports what it saw and nothing more.

`format_gpu` renders the three shapes and nothing else:

- Fields present are joined with ` · `, in this order: `busy 12%`,
  `VRAM 1.2 / 8.0 GiB (15.0%)` (via `system.format_usage`, percent computed
  here since sysfs does not give one; a `vram_total` of 0 or a missing
  partner leaves VRAM out), `35 °C`. On today's hpz440 that is `35 °C`.
- No field present and no note: `no metrics exposed by radeon`, or
  `no metrics exposed` when the driver is unknown.
- A note: the note alone.

### Wiring

`app.py`: `run()` gains `gpu_collector: Callable[[], tuple[GpuEntry, ...]] =
collect_gpus`, injected exactly like `storage_collector`; the renderer
signature grows a third positional argument, `gpus`.

`ui.py`: `build_dashboard(metrics, storage=(), gpus=())`. GPU rows follow
the storage rows, before IPv4. An empty `gpus` renders one row, `GPU` /
`none detected`.

`snapshot.py`: `gpus: tuple[GpuEntry, ...] = ()`, next to `storage`.

`webapp.py`: `collect_snapshot(..., gpus: Callable[[], tuple[GpuEntry,
...]] = collect_gpus)` and `gpus=gpus()` in the snapshot.

`web.py`: `_gpu_section(snapshot)`, a third cell in the resource grid after
storage, with the same three states storage has: not yet probed, empty
(`none detected`), rows.

### Tests

`tests/test_gpu.py`, on trees built under `tmp_path` with no real sysfs:

- radeon-only card: driver and temperature, no busy, no VRAM; row reads
  `35 °C`.
- amdgpu-shaped card: all five files; row carries busy, VRAM with percent,
  temperature, in that order.
- connector nodes and `renderD128` alongside `card0` yield one entry.
- two cards sort `card0`, `card1`.
- a card with no readable metric files renders `no metrics exposed by
  <driver>`; with `uevent` also missing, the label is bare and the note reads
  `no metrics exposed`.
- a `temp1_input` holding `garbage` leaves `temp_c` `None` and keeps the
  row.
- `drm_root` that does not exist yields the single `unavailable` entry.
- an empty `drm_root` yields an empty tuple.

`tests/test_ui.py` and `tests/test_web.py`: a GPU row appears with its
formatted value; an empty tuple shows `none detected`; the web section
before the first cycle says nothing has been collected.

`tests/test_app.py` and `tests/test_webapp.py`: the injected fake GPU
collector is called and its entries reach the renderer / snapshot.

### Out of scope

Vendor CLIs (`nvidia-smi`, `rocm-smi`), PCI ID lookup for a marketing name,
fan speed and power draw, per-process usage, and any threshold or alert. Each
is a later addition behind the same entry shape if a need appears.

No ADR: this adds a collector under the rules CLAUDE.md already states --
collect, render, coordinate, one job each; degrade, never raise; absence of
evidence is never a finding. Storage was added the same way.
