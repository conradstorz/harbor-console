"""Reading and updating a project's `.harbor.toml` declaration.

`want` is human-owned and never rewritten here. `assigned` is harbor-owned and
is written back surgically, one line at a time, so that comments and layout in
somebody else's repository survive being edited by this tool.

Every string that leaves this module is checked at the door. A `.harbor.toml`
lives in a repository this tool does not own, and its `project`, `host`, `name`
and `addr` are interpolated verbatim into the ledger's TOML strings and into
another project's `.env`. A quote or a newline in one of them wrote a ledger no
later command could load -- the tool could not start again until somebody
hand-edited `services.toml`. Escaping in the emitter alone would not do: the
same name flows on into `keys.env_var_name` and the `.env` fence, so the value
is refused here rather than made safe for one consumer out of several.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from harbor_console.ports.atomic import write_text_atomic
from harbor_console.ports.keys import ANY_ADDR, env_var_name

#: What a `project`, `host` or port `name` may contain. Deliberately narrower
#: than anything downstream strictly needs: these are identifiers that end up in
#: TOML strings, environment variable names and file paths, and a conservative
#: charset is the one rule that holds for all three.
_IDENTIFIER = re.compile(r"[A-Za-z0-9._-]+")

#: A bind address. Same reasoning, one charset wider so that IPv4, IPv6 and a
#: hostname all pass while a quote, a backslash or a newline does not.
_ADDR = re.compile(r"[A-Za-z0-9.:_-]+")


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
        _check(path, field, data[field], _IDENTIFIER)

    ports: list[PortRequest] = []
    seen: set[str] = set()
    variables: dict[str, str] = {}
    for entry in data.get("port", []):
        if "name" not in entry:
            raise DeclarationError(f"{path}: a [[port]] has no 'name'")
        name = entry["name"]
        _check(path, "port name", name, _IDENTIFIER)
        if name in seen:
            raise DeclarationError(f"{path}: duplicate port name '{name}'")
        seen.add(name)

        # Two port names may differ and still derive one variable: `web-ui` and
        # `web_ui` are both `HARBOR_PORT_WEB_UI`. Both would be leased and both
        # written as `assigned`, while `.env` published only the last of them --
        # so the other container fell back to its compose default, on a port the
        # ledger had leased to somebody else. Silently. This is where that stops.
        variable = env_var_name(name)
        if variable in variables:
            raise DeclarationError(
                f"{path}: port names '{variables[variable]}' and '{name}' both "
                f"publish {variable}; only one of them could ever reach .env"
            )
        variables[variable] = name

        addr = entry.get("addr", ANY_ADDR)
        _check(path, "addr", addr, _ADDR)

        ports.append(
            PortRequest(
                name=name,
                want=entry.get("want"),
                assigned=entry.get("assigned"),
                addr=addr,
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


def reject_duplicate_declarations(declarations: Sequence[Declaration]) -> None:
    """Refuse two directories that declare the same project on the same host.

    `(project, host)` is the identity every lease is keyed on, so two
    directories claiming it are one project as far as the ledger is concerned
    and two as far as the filesystem is concerned. Keying declarations by
    project name lets the last one read win silently: the lease is recorded for
    one directory while `.env` and `.harbor.toml` are written into the other,
    and a port announced as granted ends up leased to nobody.

    The trigger is entirely ordinary -- a `-backup` or `-old` copy, or a second
    worktree of a participating repository sitting as a sibling -- because
    `.harbor.toml` travels with the copy. It is not this tool's place to guess
    which of the two is the real one, so it names both and stops.
    """
    seen: dict[tuple[str, str], Declaration] = {}
    for declaration in declarations:
        key = (declaration.project, declaration.host)
        first = seen.get(key)
        if first is not None:
            raise DeclarationError(
                f"{first.path} and {declaration.path} both declare project "
                f"'{declaration.project}' on host '{declaration.host}'; one project "
                f"cannot live in two directories -- remove or rename the copy"
            )
        seen[key] = declaration


def _check(path: Path, field: str, value: object, allowed: re.Pattern[str]) -> None:
    """Refuse a field that is not a non-empty string of `allowed` characters.

    The whole value must match: a partial match is what lets a newline or a
    quote through in the tail of an otherwise ordinary-looking name.
    """
    if not isinstance(value, str) or not allowed.fullmatch(value):
        raise DeclarationError(
            f"{path}: {field} {value!r} is not usable; it must be a non-empty "
            f"string of {allowed.pattern} characters"
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
