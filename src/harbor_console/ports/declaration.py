"""Reading and updating a project's `.harbor.toml` declaration.

`want` is human-owned and never rewritten here. `assigned` is harbor-owned and
is written back surgically, one line at a time, so that comments and layout in
somebody else's repository survive being edited by this tool.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from harbor_console.ports.atomic import write_text_atomic
from harbor_console.ports.keys import ANY_ADDR


class DeclarationError(Exception):
    """A `.harbor.toml` is unreadable or malformed."""


@dataclass(frozen=True)
class PortRequest:
    """One port a project asks for."""

    name: str
    want: int | None
    assigned: int | None
    addr: str
    container: str | None
    health_path: str
    hcstatus_path: str | None
    description: str


@dataclass(frozen=True)
class Declaration:
    """One project's request, as found in its repository."""

    project: str
    host: str
    path: Path
    ports: tuple[PortRequest, ...]


def load_declaration(path: Path) -> Declaration:
    """Read and validate one `.harbor.toml`."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise DeclarationError(f"{path}: {exc}") from exc
    except OSError as exc:
        raise DeclarationError(f"{path}: {exc}") from exc

    for field in ("project", "host"):
        if field not in data:
            raise DeclarationError(f"{path}: missing '{field}'")

    ports: list[PortRequest] = []
    seen: set[str] = set()
    for entry in data.get("port", []):
        if "name" not in entry:
            raise DeclarationError(f"{path}: a [[port]] has no 'name'")
        name = entry["name"]
        if name in seen:
            raise DeclarationError(f"{path}: duplicate port name '{name}'")
        seen.add(name)
        ports.append(
            PortRequest(
                name=name,
                want=entry.get("want"),
                assigned=entry.get("assigned"),
                addr=entry.get("addr", ANY_ADDR),
                container=entry.get("container"),
                health_path=entry.get("health_path", "/"),
                hcstatus_path=entry.get("hcstatus_path"),
                description=entry.get("description", ""),
            )
        )

    return Declaration(
        project=data["project"],
        host=data["host"],
        path=path,
        ports=tuple(ports),
    )


def write_assigned(path: Path, port_name: str, assigned: int) -> None:
    """Set `assigned` inside the named [[port]] block, leaving all else alone.

    Written atomically, so a failed write leaves the project's `.harbor.toml`
    as it was rather than truncated.
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    blocks = _port_block_bounds(lines)

    for start, end in blocks:
        if _block_name(lines[start:end]) != port_name:
            continue
        _set_field(lines, start, end, assigned)
        write_text_atomic(path, "".join(lines))
        return

    raise DeclarationError(f"{path}: no [[port]] named '{port_name}'")


def _port_block_bounds(lines: list[str]) -> list[tuple[int, int]]:
    """Return (start, end) line indices for each [[port]] block.

    A block ends at the next top-level table header of any kind
    (`[[port]]`, `[[other]]`, `[other]`, ...) or at end of file - never
    past it - so a table that follows a `[[port]]` block is never mistaken
    for part of that block.
    """
    starts = [i for i, line in enumerate(lines) if line.strip() == "[[port]]"]
    bounds = []
    for start in starts:
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if _is_table_header(lines[index]):
                end = index
                break
        bounds.append((start, end))
    return bounds


def _is_table_header(line: str) -> bool:
    """True if `line` opens a top-level TOML table, e.g. `[foo]` or `[[foo]]`."""
    stripped = line.strip()
    return len(stripped) > 2 and stripped.startswith("[") and stripped.endswith("]")


def _block_name(block: list[str]) -> str | None:
    for line in block:
        key, _, value = line.partition("=")
        if key.strip() == "name":
            return value.split("#")[0].strip().strip('"')
    return None


def _set_field(lines: list[str], start: int, end: int, assigned: int) -> None:
    """Replace an existing `assigned` line, or insert one after `name`."""
    for index in range(start, end):
        if lines[index].partition("=")[0].strip() == "assigned":
            lines[index] = f"assigned      = {assigned}\n"
            return

    for index in range(start, end):
        if lines[index].partition("=")[0].strip() == "name":
            _ensure_trailing_newline(lines, index)
            lines.insert(index + 1, f"assigned      = {assigned}\n")
            return

    _ensure_trailing_newline(lines, start)
    lines.insert(start + 1, f"assigned      = {assigned}\n")


def _ensure_trailing_newline(lines: list[str], index: int) -> None:
    """Append a newline to `lines[index]` if it lacks one (e.g. last line of file)."""
    if not lines[index].endswith("\n"):
        lines[index] += "\n"
