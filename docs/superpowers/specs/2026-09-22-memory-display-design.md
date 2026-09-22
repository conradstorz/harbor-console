# Memory display: a percentage with no scale, and no word about swap

Date: 2026-09-22
Status: approved, not yet implemented

## The problem

Both surfaces report memory as one number. On hpz440 the console reads:

```
Memory utilization    13.0%
```

That figure is correct -- it is `(total - available) / total`, psutil's own
basis, which counts reclaimable page cache as free -- and it is not enough to
act on.

1. **No scale.** 13% of what? The host has 31.3 GiB, so this is 4 GiB in use.
   On a 4 GiB box the same 13% would be half a gigabyte. The number cannot be
   read without already knowing the answer.

2. **Nothing about swap.** The host has 8 GiB of swap and the console never
   mentions it. A host that has begun swapping is in trouble in a way no CPU
   or memory percentage shows, and this is a console whose job is the
   at-a-glance verdict on a machine running seventeen containers.

Ground truth at design time, from `free -h` on hpz440:

```
              total  used  free  shared  buff/cache  available
Mem:           31Gi  4.0Gi  10Gi   168Mi       17Gi       27Gi
Swap:         8.0Gi     0B 8.0Gi
```

A separate defect found while reading the code: **`ui.py` has no tests.**
`build_dashboard` is referenced only by `app.py` and itself; no test file
imports it. It is the one module in this project with no coverage, and
CLAUDE.md describes the metrics dict as "the contract between `system` and
`ui`" while nothing enforces that contract. This work edits that module, so
it covers it.

## What gets built

### `system.py` -- one formatter and two keys

A formatter beside `format_uptime`, following that precedent: this project
already lets the collector emit display-ready text for uptime, and both
surfaces print it verbatim.

```python
def format_bytes(n: int) -> str:
    """Gibibytes with one decimal, as `free -h` counts them.

    Returns the number alone, without a unit: a summary line names `GiB` once
    for the pair rather than twice.
    """
    return f"{n / 1024**3:.1f}"
```

`collect_system_metrics()` replaces its memory key with two summaries:

```python
"memory_summary": "4.1 / 31.3 GiB (13.0%)",
"swap_summary":   "0.0 / 8.0 GiB (0.0%)",
```

`memory_utilization` is removed. Once both renderers read the summaries,
nothing reads it -- the same reason `Snapshot.listeners` went in [ADR
18](../../adr/0018-show-the-full-listening-inventory.md)'s branch. CLAUDE.md
requires that changing a key updates the collector, the renderer and
`tests/test_system.py` together, so all three move in one commit.

Three decisions, pinned here rather than discovered during implementation:

- **Used is `total - available`.** Not `total - free - buff/cache`, which is
  what `free`'s "used" column shows. `total - available` is the basis psutil's
  `.percent` already uses, so the bytes and the percentage on one line agree
  with each other. On the numbers above it gives 4.1 GiB where `free` says
  4.0, since `free` counts 168 MiB of shared memory differently; a line
  whose two halves disagree would be worse than a line that differs from
  another tool by a rounding's width.
- **A host with no swap reads `none configured`**, not `0.0 / 0.0 GiB (0.0%)`.
  Zero of zero reads like a bug, and some hosts legitimately have no swap.
- **`psutil.swap_memory()` degrades to `unavailable`.** It can raise on some
  platforms, and collectors in this project never raise on a hostile
  environment -- the same stance as `get_docker_container_count()` returning 0
  and `get_ipv4_address()` falling back to loopback. The existing
  `virtual_memory()` call is unguarded and stays that way: guarding it is a
  question about the whole collector, not about memory.

Percentages stay `.1f` throughout, so `(13.0%)` rather than `(13%)`, matching
the existing CPU and Disk rows rather than introducing a second convention.

### `ui.py` -- two rows where one was

```python
table.add_row("Memory", str(metrics["memory_summary"]))
table.add_row("Swap", str(metrics["swap_summary"]))
```

in the position memory occupies now, between CPU and Disk. These two labels
drop the word "utilization", since the value is no longer a bare percentage;
`CPU utilization` and `Disk utilization` are left alone. The result is
slightly mixed, and each label now describes what sits beside it.

### `web.py` -- the same two rows, and a magic index removed

`_host_table` gains the same `Memory` and `Swap` rows, replacing its
`f"{float(metrics['memory_utilization']):.1f}%"` line.

The existing code then has a trap. It builds the row list and inserts the
tailnet row by position:

```python
    if snapshot.tailnet_address is not None:
        rows.insert(5, ("Tailnet", snapshot.tailnet_address))
```

Index 5 currently lands between IPv4 and Containers. Adding a Swap row above
it shifts that silently to between Disk and IPv4, and no test would catch the
change, because no test pins where the tailnet row sits. The fix is to place
the tailnet row in the list directly rather than by index, and to pin its
position with a test. That is a change to code this work touches, not an
unrelated refactor.

## Testing

TDD, tests first.

- **`tests/test_ui.py`, new** -- the module's first coverage. Render
  `build_dashboard` through a recording `rich.console.Console` and assert each
  metric's value appears. Then the test that matters most: call
  `build_dashboard(collect_system_metrics())` with psutil monkeypatched, which
  fails loudly if collector and renderer ever disagree about a key. That is
  the contract CLAUDE.md describes and nothing currently enforces.
- **`tests/test_system.py`** -- the expected dict updates to the new keys;
  new cases for `format_bytes`, for a host with no swap (`none configured`),
  and for `swap_memory()` raising (`unavailable`).
- **`tests/test_web.py`** -- `Memory` and `Swap` appear in the host table with
  their summaries, and the tailnet row lands between IPv4 and Containers.

## Scope

No new dependency: `psutil.swap_memory()` is in the library already used for
every other metric. No colors, no bar graphs, no thresholds, no alerting, no
configuration -- the console stays a table of facts, and
`founding_document.txt` defers all of those deliberately. No ADR: this changes
how one existing metric is displayed and follows the existing `format_uptime`
precedent for where the formatting lives. It settles no question that a future
reader would otherwise have to re-litigate.

A bar or other visual indicator of pressure was considered and rejected for
now. It would read faster than digits, and it is the kind of presentation
change this project's minimal stance asks for a demonstrated need first --
the need here was "I cannot tell what 13% means", which numbers answer.
