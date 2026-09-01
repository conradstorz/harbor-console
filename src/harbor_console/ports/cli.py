"""`harbor-console ports` -- the only thing in the allocator that writes.

`scan` reports and writes nothing. `sync` applies. `sync --new-only` grants new
requests but withholds anything that would change an existing assignment, which
is what the scheduled task runs: a timer must never renumber a project you are
mid-deploy on.

Writing is all-or-nothing *per project*. The ledger records that a project holds
a port, and the project's own `.env` is what actually makes the container bind
it; a lease with no matching `.env` is worse than no lease at all. So every
project's files are validated before anything is written, and a project whose
files cannot all be written is dropped from the run entirely -- its lease
included -- rather than left half-done. Its neighbours are still served.
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
    warnings = _compose_warnings(declarations, decisions)

    if args.command == "scan":
        _report(changes, warnings, out, applied=False)
        return EXIT_PENDING if changes or warnings else EXIT_OK

    if changes and not live.complete:
        print(
            "live host state is incomplete (no /ports.json); refusing to grant a port "
            "that cannot be verified as unheld. Nothing written.",
            file=out,
        )
        return EXIT_PENDING

    new_only = getattr(args, "new_only", False)
    withheld = [
        decision for decision in changes if new_only and decision.action == "reassign"
    ]
    withheld_keys = {(decision.project, decision.port_name) for decision in withheld}
    applied = [
        decision
        for decision in changes
        if (decision.project, decision.port_name) not in withheld_keys
    ]

    env_values = _effective_env(decisions, withheld_keys, leases, declarations)
    by_project = {declaration.project: declaration for declaration in declarations}

    # Validate before writing: a project whose `.env` fence is corrupted is
    # dropped whole, so it never gets a lease it cannot honour.
    broken = _unwritable_projects(applied, by_project, env_values, out)
    if broken:
        applied = [decision for decision in applied if decision.project not in broken]

    try:
        _write(applied, by_project, env_values, leases, ledger_path, today)
    except (DeclarationError, EnvFenceError, OSError) as exc:
        print(f"error: {exc}", file=out)
        return EXIT_ERROR

    _report(applied, warnings, out, applied=True)

    for decision in withheld:
        print(
            f"withheld {decision.project}/{decision.port_name}: would move to "
            f"{decision.port} -- run `harbor-console ports sync`",
            file=out,
        )

    if broken:
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
    """Projects that cannot be written, reported and named before anything is.

    Only `.env` can realistically refuse: a corrupted managed fence is not
    repaired by guessing, because a wrong guess destroys secrets that live
    outside it.
    """
    broken: set[str] = set()

    for project in sorted({decision.project for decision in applied}):
        env_path = by_project[project].path.parent / ".env"
        existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        try:
            envfile.apply_fence(existing, env_values.get(project, {}))
        except EnvFenceError as exc:
            print(
                f"error: {project}: {env_path} has a corrupted harbor-console fence "
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
) -> None:
    """Apply accepted decisions: ledger first, then each project's own files."""
    if not applied:
        return

    updated = apply_decisions(leases, applied, today)
    if dumps_leases(updated) != dumps_leases(leases):
        save_leases(ledger_path, updated)

    for decision in applied:
        declaration = by_project[decision.project]
        write_assigned(declaration.path, decision.port_name, decision.port)

    for project in sorted({decision.project for decision in applied}):
        project_dir = by_project[project].path.parent
        envfile.write_env(project_dir / ".env", env_values.get(project, {}))
        explainer.write_explainer(project_dir / "HARBOR_PORTS.md")


def _compose_warnings(
    declarations: Sequence[Declaration], decisions: Sequence[Decision]
) -> list[str]:
    """`.env` is usually gitignored, so a stale compose default is what a clone gets."""
    assigned = {
        (decision.project, env_var_name(decision.port_name)): decision.port
        for decision in decisions
    }
    warnings: list[str] = []

    for declaration in declarations:
        for published in compose.published_ports(declaration.path.parent):
            if published.var is None or published.default is None:
                continue
            expected = assigned.get((declaration.project, published.var))
            if expected is not None and published.default != expected:
                warnings.append(
                    f"{declaration.project}: {published.file.name} defaults "
                    f"{published.var} to {published.default}, assigned {expected}"
                )

    return warnings


def _report(
    changes: Sequence[Decision], warnings: Sequence[str], out: TextIO, applied: bool
) -> None:
    """Print what happened, or what would happen, one line per change."""
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

    if not changes and not warnings:
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
