"""`harbor-console ports` -- the only thing in the allocator that writes.

`scan` reports and writes nothing. `sync` applies. `sync --new-only` grants new
requests but withholds anything that would change an existing assignment, which
is what the scheduled task runs: a timer must never renumber a project you are
mid-deploy on.

The ledger records that a project holds a port; the project's own `.env` is what
actually makes the container bind it. A lease with no matching `.env` is worse
than no lease at all, and two mechanisms guard against one -- neither of which is
a blanket pre-flight check of every file:

* Each affected project's `.env` is read and its managed fence parsed *before*
  anything is written, because a corrupted fence must not be guessed at: a wrong
  guess destroys secrets living outside it. A project whose `.env` is corrupted,
  unreadable, or not valid UTF-8 is named, dropped from the run whole -- no
  lease, no `assigned`, no files -- and the command exits non-zero.

* Whether `.harbor.toml` and `HARBOR_PORTS.md` can be written is *not* checked in
  advance; nothing short of writing them proves it. So the surviving projects are
  written one at a time -- declaration, then `.env`, then explainer -- and if any
  of those fails, the ledger is re-saved with that project's decisions removed.
  The project ends the run holding no lease, though files already written for it
  are not rolled back, and the error says so. Its neighbours, before and after it
  in the run, are unaffected.

Whatever did land is reported before any error line, so an operator is never left
guessing which projects were served.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import TextIO

from harbor_console.ports import compose, discovery, envfile, explainer
from harbor_console.ports.allocate import BandExhausted, Decision, apply_decisions, decide
from harbor_console.ports.declaration import (
    Declaration,
    DeclarationError,
    load_declaration,
    write_assigned,
)
from harbor_console.ports.envfile import EnvFenceError
from harbor_console.ports.keys import env_var_name
from harbor_console.ports.ledger import Lease, LedgerError, dumps_leases, load_leases, save_leases
from harbor_console.ports.live import LiveState, LiveUnavailable, fetch_live

PORTS_URL_DEFAULT = "http://hpz440:8090/ports.json"

EXIT_OK = 0
EXIT_PENDING = 1
EXIT_ERROR = 2

#: A decision's identity within one run: ``(project, port_name)``. Decisions are
#: frozen dataclasses and so comparable, but keying on identity keeps "is this
#: one withheld?" independent of every other field.
_Key = tuple[str, str]

#: Everything writing one project's files can raise. ``UnicodeDecodeError`` is a
#: ``ValueError``, not an ``OSError``, and is named for exactly that reason:
#: somebody else's `.env` may legitimately hold a cp1252 password.
_WRITE_FAILURES = (DeclarationError, EnvFenceError, OSError, UnicodeDecodeError)


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser for `harbor-console ports`."""
    parser = argparse.ArgumentParser(prog="harbor-console ports")
    parser.add_argument("--ports-url", default=PORTS_URL_DEFAULT)
    subcommands = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("scan", "report pending grants, conflicts and drift"),
        ("sync", "apply grants and reassignments"),
        ("show", "print the current lease table"),
    ):
        subcommand = subcommands.add_parser(name, help=help_text)
        # Accepted after the subcommand as well as before it. SUPPRESS keeps the
        # subparser from overwriting a value given before the subcommand with its
        # own default, which is argparse's usual trap with shared options.
        subcommand.add_argument("--ports-url", default=argparse.SUPPRESS)
        if name == "sync":
            subcommand.add_argument(
                "--new-only",
                action="store_true",
                help="grant new requests only; withhold anything already assigned",
            )

    return parser


def run(
    argv: Sequence[str],
    root: Path,
    ledger_path: Path,
    live: LiveState,
    today: date,
    out: TextIO,
) -> int:
    """Execute one command against an explicit tree, ledger and host state."""
    args = _parser().parse_args(list(argv))

    try:
        leases = load_leases(ledger_path)
        declarations = [load_declaration(path) for path in discovery.find_declarations(root)]
    except (LedgerError, DeclarationError) as exc:
        print(f"error: {exc}", file=out)
        return EXIT_ERROR

    if args.command == "show":
        return _show(leases, out)

    try:
        decisions = decide(declarations, leases, live, today)
    except BandExhausted as exc:
        print(f"error: {exc}", file=out)
        return EXIT_ERROR

    changes = [decision for decision in decisions if decision.action != "keep"]

    new_only = getattr(args, "new_only", False)
    withheld = [
        decision for decision in changes if new_only and decision.action == "reassign"
    ]
    withheld_keys = {(decision.project, decision.port_name) for decision in withheld}

    # Both the `.env` writer and the drift warnings must speak of the port a
    # project will actually be on after this run. For a withheld decision that is
    # the number it already holds, not the move that was just refused.
    env_values = _effective_env(decisions, withheld_keys, leases, declarations)
    warnings = _compose_warnings(declarations, env_values)

    if args.command == "scan":
        _report(changes, warnings, out, applied=False)
        return EXIT_PENDING if changes or warnings else EXIT_OK

    if changes and not live.complete:
        print(
            "live host state is incomplete (no /ports.json); refusing to grant a port "
            "that cannot be verified as unheld. Nothing written.",
            file=out,
        )
        # The drift warnings are already computed, and are true whether or not
        # anything is granted; discarding them here would hide a real fault
        # behind a transient one.
        _report([], warnings, out, applied=False, outstanding=True)
        return EXIT_PENDING

    applied = [
        decision
        for decision in changes
        if (decision.project, decision.port_name) not in withheld_keys
    ]
    by_project = {declaration.project: declaration for declaration in declarations}

    # Validate before writing: a project whose `.env` cannot be read, or whose
    # fence is corrupted, is dropped whole -- it never gets a lease it cannot
    # honour.
    broken = _unwritable_projects(applied, by_project, env_values, out)
    if broken:
        applied = [decision for decision in applied if decision.project not in broken]

    written, failures = _write(applied, by_project, env_values, leases, ledger_path, today)

    _report(
        written,
        warnings,
        out,
        applied=True,
        outstanding=bool(withheld or broken or failures),
    )

    for failure in failures:
        print(f"error: {failure}", file=out)

    for decision in withheld:
        print(
            f"withheld {decision.project}/{decision.port_name}: would move to "
            f"{decision.port} -- run `harbor-console ports sync`",
            file=out,
        )

    if broken or failures:
        return EXIT_ERROR
    return EXIT_PENDING if withheld or warnings else EXIT_OK


def _effective_env(
    decisions: Sequence[Decision],
    withheld_keys: set[_Key],
    leases: Sequence[Lease],
    declarations: Sequence[Declaration],
) -> dict[str, dict[str, str]]:
    """The variables each project's `.env` should publish, per project.

    Every declared port contributes, not only the ones changing: the fence is
    rewritten wholesale, so omitting an untouched port would delete it. A
    withheld port publishes the number it *currently* holds, never the move that
    was refused -- otherwise the run that declined to renumber a project would
    renumber it anyway, through the back door.
    """
    values: dict[str, dict[str, str]] = {}

    for decision in decisions:
        if (decision.project, decision.port_name) in withheld_keys:
            port = _current_port(decision, leases, declarations)
            if port is None:
                continue
        else:
            port = decision.port
        variable = env_var_name(decision.port_name)
        values.setdefault(decision.project, {})[variable] = str(port)

    return values


def _current_port(
    decision: Decision,
    leases: Sequence[Lease],
    declarations: Sequence[Declaration],
) -> int | None:
    """The port a withheld decision's port already holds, if it holds one."""
    for lease in leases:
        if (lease.project, lease.name, lease.host) == (
            decision.project,
            decision.port_name,
            decision.host,
        ):
            return lease.port

    for declaration in declarations:
        if declaration.project != decision.project:
            continue
        for request in declaration.ports:
            if request.name == decision.port_name:
                return request.assigned

    return None


def _unwritable_projects(
    applied: Sequence[Decision],
    by_project: dict[str, Declaration],
    env_values: dict[str, dict[str, str]],
    out: TextIO,
) -> set[str]:
    """Projects whose `.env` refuses the write, named before anything is written.

    `.env` is the one file that can be known unwritable in advance, and the one
    worth knowing about. A corrupted managed fence is not repaired by guessing,
    because a wrong guess destroys secrets that live outside it; and a file that
    cannot be read as UTF-8 -- a cp1252 password in somebody else's repository --
    cannot be rewritten without destroying it either. Both are reported here
    rather than escaping as a traceback.
    """
    broken: set[str] = set()

    for project in sorted({decision.project for decision in applied}):
        env_path = by_project[project].path.parent / ".env"
        try:
            existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
            envfile.apply_fence(existing, env_values.get(project, {}))
        except EnvFenceError as exc:
            print(
                f"error: {project}: {env_path} has a corrupted harbor-console fence "
                f"({exc}); nothing written for {project}. Repair it by hand.",
                file=out,
            )
            broken.add(project)
        except (OSError, UnicodeDecodeError) as exc:
            print(
                f"error: {project}: {env_path} cannot be read as UTF-8 text "
                f"({exc}); nothing written for {project}. Repair it by hand.",
                file=out,
            )
            broken.add(project)

    return broken


def _write(
    applied: Sequence[Decision],
    by_project: dict[str, Declaration],
    env_values: dict[str, dict[str, str]],
    leases: Sequence[Lease],
    ledger_path: Path,
    today: date,
) -> tuple[list[Decision], list[str]]:
    """Apply accepted decisions and report what landed. Raises nothing.

    Returns the decisions actually written, and one message per project that
    could not be written completely. The ledger is saved first, so an interrupted
    run leaves a port reserved to its rightful holder rather than named in some
    project's `.env` while the allocator still believes it free. Each project's
    own files are then written together, and a project that fails has its
    decisions taken back out of the ledger -- it ends the run holding no lease,
    and its neighbours are untouched by its failure.
    """
    if not applied:
        return [], []

    updated = apply_decisions(leases, applied, today)
    ledger_written = dumps_leases(updated) != dumps_leases(leases)
    try:
        if ledger_written:
            save_leases(ledger_path, updated)
    except OSError as exc:
        return [], [f"{ledger_path}: {exc}; nothing was written"]

    written: list[Decision] = []
    failed: set[str] = set()
    messages: list[str] = []

    for project in sorted({decision.project for decision in applied}):
        mine = [decision for decision in applied if decision.project == project]
        try:
            _write_project(by_project[project], mine, env_values.get(project, {}))
        except _WRITE_FAILURES as exc:
            failed.add(project)
            messages.append(
                f"{project}: {exc}; {project} was left holding no lease -- any file "
                f"already written for it was not rolled back. Fix the cause and re-run."
            )
        else:
            written.extend(mine)

    if failed and ledger_written:
        kept = [decision for decision in applied if decision.project not in failed]
        try:
            save_leases(ledger_path, apply_decisions(leases, kept, today))
        except OSError as exc:
            messages.append(
                f"{ledger_path}: {exc}; a lease may remain for "
                f"{', '.join(sorted(failed))} with no matching .env"
            )

    return written, messages


def _write_project(
    declaration: Declaration,
    decisions: Sequence[Decision],
    values: dict[str, str],
) -> None:
    """Write one project's declaration, `.env` and explainer, in that order."""
    for decision in decisions:
        write_assigned(declaration.path, decision.port_name, decision.port)

    project_dir = declaration.path.parent
    envfile.write_env(project_dir / ".env", values)
    explainer.write_explainer(project_dir / "HARBOR_PORTS.md")


def _compose_warnings(
    declarations: Sequence[Declaration], env_values: dict[str, dict[str, str]]
) -> list[str]:
    """`.env` is usually gitignored, so a stale compose default is what a clone gets.

    Compared against the *effective* number -- what `.env` publishes after this
    run -- rather than against the decision, so a withheld reassignment is not
    reported as drift from a port the run has just refused to move it to.
    """
    warnings: list[str] = []

    for declaration in declarations:
        effective = env_values.get(declaration.project, {})
        for published in compose.published_ports(declaration.path.parent):
            if published.var is None or published.default is None:
                continue
            expected = effective.get(published.var)
            if expected is not None and published.default != int(expected):
                warnings.append(
                    f"{declaration.project}: {published.file.name} defaults "
                    f"{published.var} to {published.default}, assigned {expected}"
                )

    return warnings


def _report(
    changes: Sequence[Decision],
    warnings: Sequence[str],
    out: TextIO,
    applied: bool,
    outstanding: bool = False,
) -> None:
    """Print what happened, or what would happen, one line per change.

    `outstanding` says something is still pending or wrong -- a withheld port, a
    dropped project, a failed write -- so that "up to date" is suppressed rather
    than printed immediately above the lines that contradict it.
    """
    verb = "wrote" if applied else "would write"
    for decision in changes:
        line = f"{verb} {decision.project}/{decision.port_name} = {decision.port}"
        if decision.incumbent is not None:
            line += f"  ({decision.reason}, held since {decision.incumbent.granted.isoformat()})"
        else:
            line += f"  ({decision.reason})"
        print(line, file=out)

    for warning in warnings:
        print(f"warning: {warning}", file=out)

    if not changes and not warnings and not outstanding:
        print("up to date", file=out)


def _show(leases: Sequence[Lease], out: TextIO) -> int:
    """Print the lease table, host by host, and write nothing."""
    for lease in sorted(leases, key=lambda item: (item.host, item.port, item.addr)):
        print(
            f"{lease.host} {lease.addr}:{lease.port}  {lease.project}/{lease.name}"
            f"  since {lease.granted.isoformat()}",
            file=out,
        )
    return EXIT_OK


def main(argv: Sequence[str]) -> int:
    """Entry point for `harbor-console ports ...`."""
    try:
        args, _ = _parser().parse_known_args(list(argv))
    except SystemExit as exc:
        return int(exc.code or 0)

    root = discovery.tree_root()
    ledger_path = Path(__file__).resolve().parents[3] / "services.toml"

    try:
        live = fetch_live(args.ports_url)
    except LiveUnavailable as exc:
        print(f"warning: {exc}", file=sys.stdout)
        live = LiveState(host="", listeners=(), complete=False)

    return run(argv, root, ledger_path, live, date.today(), sys.stdout)
