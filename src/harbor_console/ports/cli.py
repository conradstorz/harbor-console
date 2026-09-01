"""`harbor-console ports` -- the only thing in the allocator that writes.

`scan` reports and writes nothing. `sync` applies. `sync --new-only` grants new
requests but withholds anything that would change an existing assignment, which
is what the scheduled task runs: a timer must never renumber a project you are
mid-deploy on.

The ledger records that a project holds a port; the project's own `.env` is what
actually makes the container bind it. A lease with no matching `.env` is worse
than no lease at all, so `sync` writes a project's files whenever they disagree
with the ledger -- not only when a port moves. `.env` is gitignored, so *every*
fresh clone of a participating project starts without one while its lease stands
and its decisions are all "keep"; a run that wrote only what changed would say
"up to date" over a project whose compose file is about to interpolate its own
default, on a port leased to somebody else. That is this tool's founding
failure, at exit 0. A repair is reported as a repair, distinctly from a grant,
because restoring a lease a project already holds is not the same event as
handing it a new one. A tree that already matches is still a genuine no-op: no
file is opened for writing, and the ledger's mtime does not move.

Two further mechanisms guard a lease against having no matching `.env` --
neither of which is a blanket pre-flight check of every file:

* Each affected project's `.env` is read and its managed fence parsed *before*
  anything is written, because a corrupted fence must not be guessed at: a wrong
  guess destroys secrets living outside it. A project whose `.env` is corrupted,
  unreadable, or not valid UTF-8 is named, dropped from the run whole -- no
  lease, no `assigned`, no files -- and the command exits non-zero.

* Whether `.harbor.toml` and `HARBOR_PORTS.md` can be written is *not* checked in
  advance; nothing short of writing them proves it. So the surviving projects are
  written one at a time -- explainer, then declaration, then `.env` -- and if any
  of those fails, the ledger is re-saved with *this run's* decisions for that
  project removed. It ends the run holding exactly the lease it held before the
  run, which may be none, and which may disagree with a file already written for
  it, since those are not rolled back; the error says so. Its neighbours, before
  and after it in the run, are unaffected. `.env` is written last precisely so
  that every failure before it leaves that withdrawal truthful -- nothing
  publishes a port the ledger does not grant.

Each of those files is replaced atomically (see `atomic.write_text_atomic`), so a
failed write leaves the previous file intact rather than an empty one. These are
other people's repositories, and their `.env` is not this tool's to lose.

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
    reject_duplicate_declarations,
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

#: One project's outstanding repairs: why its own files disagree with the
#: ledger, in words fit to print. Keyed by project name.
_Repairs = dict[str, list[str]]

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
    except LedgerError as exc:
        print(f"error: {exc}", file=out)
        return EXIT_ERROR

    # `show` reads no declaration. It is the one command that answers "what is
    # leased right now", and it has to keep answering while somebody else's
    # `.harbor.toml` is broken -- that is precisely when an operator needs it.
    if args.command == "show":
        return _show(leases, out)

    try:
        declarations = [load_declaration(path) for path in discovery.find_declarations(root)]
        reject_duplicate_declarations(declarations)
    except DeclarationError as exc:
        print(f"error: {exc}", file=out)
        return EXIT_ERROR

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
        # A project already in the change set is about to be written whole;
        # reporting it a second time as a repair would double-count it.
        pending = {decision.project for decision in changes}
        repairs = {
            project: reasons
            for project, reasons in _repairs_needed(declarations, env_values).items()
            if project not in pending
        }
        _report(
            changes,
            warnings,
            out,
            applied=False,
            repairs=repairs,
            incomplete=not live.complete,
        )
        return EXIT_PENDING if changes or warnings or repairs else EXIT_OK

    if changes and not live.complete:
        print(
            "live host state is incomplete (no /ports.json); refusing to grant a port "
            "that cannot be verified as unheld. Nothing written.",
            file=out,
        )
        # Drift is still worth reporting -- hiding a real fault behind a
        # transient one helps nobody -- but it must be reported against the port
        # each project is actually on, never against the grant this branch has
        # just refused to make. Telling an operator to change a compose default
        # to a number the run declined to hand out would be a guess, on the one
        # path whose whole contract is that it does not guess. Every change is
        # therefore treated exactly as a withheld decision is.
        ungranted = {(decision.project, decision.port_name) for decision in changes}
        held_values = _effective_env(decisions, ungranted, leases, declarations)
        _report(
            [],
            _compose_warnings(declarations, held_values),
            out,
            applied=False,
            outstanding=True,
            repairs=_repairs_needed(declarations, held_values),
        )
        return EXIT_PENDING

    applied = [
        decision
        for decision in changes
        if (decision.project, decision.port_name) not in withheld_keys
    ]
    by_project = {declaration.project: declaration for declaration in declarations}

    # A project with no decision to apply still belongs in the write pass when
    # its own files have drifted from the lease it already holds. `env_values`
    # has been computed for every project, keep-only ones included, so the
    # comparison is against exactly what would be written.
    writing = {decision.project for decision in applied}
    repairs = {
        project: reasons
        for project, reasons in _repairs_needed(declarations, env_values).items()
        if project not in writing
    }

    # Validate before writing: a project whose `.env` cannot be read, or whose
    # fence is corrupted, is dropped whole -- it never gets a lease it cannot
    # honour, and a repair is not attempted on a file that must not be guessed
    # at either.
    broken = _unwritable_projects(
        sorted(writing | set(repairs)), by_project, env_values, out
    )
    if broken:
        applied = [decision for decision in applied if decision.project not in broken]
        repairs = {
            project: reasons
            for project, reasons in repairs.items()
            if project not in broken
        }

    written, repaired, failures = _write(
        applied, sorted(repairs), by_project, env_values, leases, ledger_path, today
    )

    _report(
        written,
        warnings,
        out,
        applied=True,
        outstanding=bool(withheld or broken or failures),
        repairs={project: repairs[project] for project in repaired},
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


def _repairs_needed(
    declarations: Sequence[Declaration],
    env_values: dict[str, dict[str, str]],
) -> _Repairs:
    """Projects whose own files no longer say what the ledger says, and why.

    Asked of *every* project, not only the ones whose ports are moving, because
    the ordinary way for a project to lose its `.env` is not a reassignment: it
    is gitignored, so a fresh clone simply has none while its lease stands and
    every decision about it is "keep". The comparison is against `env_values`,
    which is what a write would put there -- so a project that already matches
    yields nothing and is never opened for writing.

    A project with no effective port is skipped entirely: there is nothing to
    publish, and writing an empty managed fence into a `.env` that no port
    needs would be this tool inventing work in somebody else's repository.

    The reasons are phrased for an operator to read. Reading `.env` can fail --
    a directory, a cp1252 password, a corrupted fence -- and that is reported as
    the repair it is, rather than raising here; `_unwritable_projects` is what
    turns it into a refusal at write time, and `scan` must be able to say it
    while writing nothing at all.
    """
    needed: _Repairs = {}

    for declaration in declarations:
        expected = env_values.get(declaration.project, {})
        if not expected:
            continue

        project_dir = declaration.path.parent
        reasons = _env_repairs(project_dir / ".env", expected)
        if explainer.is_outdated(project_dir / "HARBOR_PORTS.md"):
            reasons.append("HARBOR_PORTS.md is missing or outdated")
        if reasons:
            needed[declaration.project] = reasons

    return needed


def _env_repairs(env_path: Path, expected: dict[str, str]) -> list[str]:
    """Why `env_path` disagrees with `expected`, or an empty list if it does not."""
    exists = env_path.exists()
    try:
        existing = env_path.read_text(encoding="utf-8") if exists else ""
    except (OSError, UnicodeDecodeError) as exc:
        return [f".env cannot be read as UTF-8 text ({exc})"]

    try:
        wanted = envfile.apply_fence(existing, expected)
    except EnvFenceError as exc:
        return [f".env has a corrupted harbor-console fence ({exc})"]

    if wanted == existing:
        return []
    return [".env is missing" if not exists else ".env does not match the ledger"]


def _unwritable_projects(
    projects: Sequence[str],
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

    `projects` is every project the run intends to write: the ones with a
    decision to apply and the ones whose files are merely being repaired. A
    repair is a write into somebody else's `.env` like any other and is held to
    the same refusal.
    """
    broken: set[str] = set()

    for project in sorted(projects):
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
    repairs: Sequence[str],
    by_project: dict[str, Declaration],
    env_values: dict[str, dict[str, str]],
    leases: Sequence[Lease],
    ledger_path: Path,
    today: date,
) -> tuple[list[Decision], list[str], list[str]]:
    """Apply accepted decisions, repair drifted files, report what landed.

    Raises nothing. Returns the decisions actually written, the projects
    repaired, and one message per project that could not be written completely.

    `repairs` names projects with nothing to decide whose own files have drifted
    from the lease they already hold. They are written like any other project
    except that no `assigned` is rewritten -- there is no decision to record, and
    a project's `.harbor.toml` is not touched to say what it already says. The ledger is saved first, so an interrupted
    run leaves a port reserved to its rightful holder rather than named in some
    project's `.env` while the allocator still believes it free. Each project's
    own files are then written together, and a project that fails has *this
    run's* decisions taken back out of the ledger -- not every lease it has. It
    therefore ends the run holding exactly the lease it held before the run,
    which is often none but is whatever was there for a project being
    reassigned, and which may disagree with a file already written for it, since
    those are not rolled back. Its neighbours are untouched by its failure.
    """
    if not applied and not repairs:
        return [], [], []

    updated = apply_decisions(leases, applied, today)
    ledger_written = dumps_leases(updated) != dumps_leases(leases)
    try:
        if ledger_written:
            save_leases(ledger_path, updated)
    except OSError as exc:
        return [], [], [f"{ledger_path}: {exc}; nothing was written"]

    written: list[Decision] = []
    repaired: list[str] = []
    failed: set[str] = set()
    messages: list[str] = []

    for project in sorted({decision.project for decision in applied} | set(repairs)):
        mine = [decision for decision in applied if decision.project == project]
        try:
            _write_project(by_project[project], mine, env_values.get(project, {}))
        except _WRITE_FAILURES as exc:
            failed.add(project)
            if mine:
                messages.append(
                    f"{project}: {exc}; every decision made for {project} in this run "
                    f"was withdrawn from the ledger, so it still holds whatever lease "
                    f"it held before the run -- which may be none, and which may "
                    f"disagree with any file already written for it, since those are "
                    f"not rolled back. Fix the cause and re-run."
                )
            else:
                # Nothing was decided for this project, so there is nothing to
                # withdraw: it holds the lease it walked in with either way. What
                # failed is the repair, and its files still disagree with that
                # lease -- which is exactly the state that needed fixing.
                messages.append(
                    f"{project}: {exc}; its lease is unchanged but its files still "
                    f"disagree with it. Fix the cause and re-run."
                )
        else:
            written.extend(mine)
            if not mine:
                repaired.append(project)

    if failed and ledger_written:
        kept = [decision for decision in applied if decision.project not in failed]
        try:
            save_leases(ledger_path, apply_decisions(leases, kept, today))
        except OSError as exc:
            messages.append(
                f"{ledger_path}: {exc}; a lease may remain for "
                f"{', '.join(sorted(failed))} with no matching .env"
            )

    return written, repaired, messages


def _write_project(
    declaration: Declaration,
    decisions: Sequence[Decision],
    values: dict[str, str],
) -> None:
    """Write one project's explainer, declaration and `.env`, in that order.

    `decisions` is empty for a repair: nothing about the project's ports is
    changing, so its `.harbor.toml` already says what a write would say and is
    left alone.

    `.env` goes last, deliberately. It is the file that makes a container
    actually bind the port, while the ledger is only the record of who is
    entitled to it -- and a project that fails here has this run's decisions
    withdrawn from the ledger. Writing `.env` last is therefore what keeps that
    withdrawal truthful: every failure before it leaves nothing publishing a
    port the ledger no longer grants. The other order would leave a project's
    `.env` naming a port the allocator believes free, which is this project's
    founding failure -- two projects on one port -- reached through the very
    mechanism meant to prevent it, and it does not heal itself: only a lease
    reserves a port, an `assigned` value does not.
    """
    project_dir = declaration.path.parent
    explainer.write_explainer(project_dir / "HARBOR_PORTS.md")

    for decision in decisions:
        write_assigned(declaration.path, decision.port_name, decision.port)

    envfile.write_env(project_dir / ".env", values)


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
    repairs: _Repairs | None = None,
    incomplete: bool = False,
) -> None:
    """Print what happened, or what would happen, one line per change.

    `outstanding` says something is still pending or wrong -- a withheld port, a
    dropped project, a failed write -- so that "up to date" is suppressed rather
    than printed immediately above the lines that contradict it.

    `repairs` are printed apart from the grants and in their own words: a repair
    restores a lease the project already holds, which is a different event from
    being handed a new port, and an operator reading "wrote alpha/web = 8080"
    has no way to tell that nothing was actually granted.

    `incomplete` says the live host state could not be verified. The caveat
    belongs here, in the reporting path, rather than at the top of `main()`:
    `sync` refuses to grant on this input, so a `scan` that prints "would write"
    without it promises something the next command will not do -- and a caller
    handing `run()` its own state never reaches `main()` at all.
    """
    if incomplete and changes:
        print(
            "note: live host state is incomplete (no /ports.json), so the grants "
            "below are unverified -- `sync` will refuse them until it is reachable.",
            file=out,
        )

    verb = "wrote" if applied else "would write"
    for decision in changes:
        line = f"{verb} {decision.project}/{decision.port_name} = {decision.port}"
        if decision.incumbent is not None:
            line += f"  ({decision.reason}, held since {decision.incumbent.granted.isoformat()})"
        else:
            line += f"  ({decision.reason})"
        print(line, file=out)

    repair_verb = "repaired" if applied else "would repair"
    for project in sorted(repairs or {}):
        print(f"{repair_verb} {project}: {'; '.join(repairs[project])}", file=out)

    for warning in warnings:
        print(f"warning: {warning}", file=out)

    if not changes and not warnings and not repairs and not outstanding:
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
