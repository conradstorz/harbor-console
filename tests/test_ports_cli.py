from __future__ import annotations

import io
import os
from datetime import date
from pathlib import Path

from harbor_console import app
from harbor_console.ports import cli
from harbor_console.ports.declaration import load_declaration
from harbor_console.ports.envfile import FENCE_END, FENCE_START
from harbor_console.ports.ledger import Lease, load_leases, save_leases
from harbor_console.ports.live import Listener, LiveState

TODAY = date(2026, 9, 1)


def make_project(root: Path, name: str, want: int, container: str | None = None) -> Path:
    project = root / name
    project.mkdir()
    body = f'project = "{name}"\nhost = "hpz440"\n\n[[port]]\nname = "web"\nwant = {want}\n'
    if container:
        body += f'container = "{container}"\n'
    (project / ".harbor.toml").write_text(body, encoding="utf-8")
    return project


def live(*pairs, complete=True):
    return LiveState(
        host="hpz440",
        listeners=tuple(Listener(a, p, c) for a, p, c in pairs),
        complete=complete,
    )


def run(argv, root, ledger_path, state=None):
    out = io.StringIO()
    code = cli.run(
        argv,
        root=root,
        ledger_path=ledger_path,
        live=state if state is not None else live(),
        today=TODAY,
        out=out,
    )
    return code, out.getvalue()


def test_scan_reports_a_pending_grant_and_writes_nothing(tmp_path: Path):
    project = make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"

    code, output = run(["scan"], tmp_path, ledger_path)

    assert code == 1
    assert "alpha" in output
    assert "8080" in output
    assert not ledger_path.exists()
    assert not (project / ".env").exists()
    assert load_declaration(project / ".harbor.toml").ports[0].assigned is None


def test_sync_writes_ledger_env_declaration_and_explainer(tmp_path: Path):
    project = make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"

    code, _ = run(["sync"], tmp_path, ledger_path)

    assert code == 0
    assert [lease.port for lease in load_leases(ledger_path)] == [8080]
    assert "HARBOR_PORT_WEB=8080" in (project / ".env").read_text(encoding="utf-8")
    assert load_declaration(project / ".harbor.toml").ports[0].assigned == 8080
    assert (project / "HARBOR_PORTS.md").is_file()


def test_sync_is_idempotent(tmp_path: Path):
    make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"
    run(["sync"], tmp_path, ledger_path)
    first = ledger_path.read_text(encoding="utf-8")

    code, _ = run(["sync"], tmp_path, ledger_path)

    assert code == 0
    assert ledger_path.read_text(encoding="utf-8") == first


def test_incumbent_keeps_the_port_and_the_newcomer_is_moved(tmp_path: Path):
    make_project(tmp_path, "gte", 8080)
    ledger_path = tmp_path / "services.toml"
    save_leases(ledger_path, [Lease("gte", "web", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5))])
    newcomer = make_project(tmp_path, "imageharbor", 8080)

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 0
    assert "gte" in output
    assert load_declaration(newcomer / ".harbor.toml").ports[0].assigned == 8100
    held = {lease.project: lease.port for lease in load_leases(ledger_path)}
    assert held == {"gte": 8080, "imageharbor": 8100}


def test_new_only_grants_new_but_withholds_a_reassignment(tmp_path: Path):
    ledger_path = tmp_path / "services.toml"
    # Only gte holds a lease. imageharbor's declaration still claims 8080, so it
    # must be reassigned -- and an unattended run must refuse to do that.
    save_leases(ledger_path, [Lease("gte", "web", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5))])
    make_project(tmp_path, "gte", 8080)
    moved = make_project(tmp_path, "imageharbor", 8080)
    (moved / ".harbor.toml").write_text(
        'project = "imageharbor"\nhost = "hpz440"\n\n[[port]]\n'
        'name = "web"\nwant = 8080\nassigned = 8080\n',
        encoding="utf-8",
    )
    fresh = make_project(tmp_path, "moonrise", 8500)

    code, output = run(["sync", "--new-only"], tmp_path, ledger_path)

    assert code == 1
    assert load_declaration(fresh / ".harbor.toml").ports[0].assigned == 8500
    assert load_declaration(moved / ".harbor.toml").ports[0].assigned == 8080
    assert "withheld" in output.lower()


def test_degraded_live_state_refuses_to_grant(tmp_path: Path):
    make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"

    code, output = run(["sync"], tmp_path, ledger_path, state=live(complete=False))

    assert code == 1
    assert "incomplete" in output.lower()
    assert not ledger_path.exists()


def test_scan_warns_when_a_compose_default_has_drifted(tmp_path: Path):
    project = make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"
    run(["sync"], tmp_path, ledger_path)
    (project / "docker-compose.yml").write_text(
        'services:\n  a:\n    ports:\n      - "${HARBOR_PORT_WEB:-9999}:80"\n',
        encoding="utf-8",
    )

    code, output = run(["scan"], tmp_path, ledger_path)

    assert code == 1
    assert "9999" in output


def test_show_lists_leases_and_writes_nothing(tmp_path: Path):
    ledger_path = tmp_path / "services.toml"
    save_leases(ledger_path, [Lease("gte", "web", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5))])

    code, output = run(["show"], tmp_path, ledger_path)

    assert code == 0
    assert "gte" in output
    assert "8080" in output


def test_a_broken_declaration_fails_without_writing_anything(tmp_path: Path):
    project = tmp_path / "bad"
    project.mkdir()
    (project / ".harbor.toml").write_text('host = "hpz440"\n', encoding="utf-8")
    ledger_path = tmp_path / "services.toml"

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 2
    assert "project" in output
    assert not ledger_path.exists()


def test_a_broken_env_fence_skips_that_project_and_spares_the_rest(tmp_path: Path):
    # `.env` holds secrets and is never backed up, so a corrupted managed fence
    # is refused rather than guessed at. The project it belongs to must be left
    # entirely untouched -- no lease, no `assigned` -- while its neighbours are
    # still served.
    broken = make_project(tmp_path, "alpha", 8080)
    (broken / ".env").write_text(
        f"SECRET=keepme\n{FENCE_START}\nHARBOR_PORT_WEB=1\n", encoding="utf-8"
    )
    healthy = make_project(tmp_path, "beta", 8500)
    ledger_path = tmp_path / "services.toml"

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 2
    assert "alpha" in output
    assert ".env" in output
    assert "SECRET=keepme" in (broken / ".env").read_text(encoding="utf-8")
    assert load_declaration(broken / ".harbor.toml").ports[0].assigned is None
    assert {lease.project: lease.port for lease in load_leases(ledger_path)} == {"beta": 8500}
    assert load_declaration(healthy / ".harbor.toml").ports[0].assigned == 8500


def test_bare_invocation_still_runs_the_dashboard(monkeypatch):
    calls: list[str] = []

    def fake_run() -> int:
        calls.append("dashboard")
        return 0

    monkeypatch.setattr(app, "run", fake_run)

    assert app.main([]) == 0
    assert calls == ["dashboard"]


def test_ports_subcommand_is_dispatched_to_the_allocator(monkeypatch):
    seen: dict[str, list[str]] = {}

    def fake_main(argv) -> int:
        seen["argv"] = list(argv)
        return 7

    monkeypatch.setattr(cli, "main", fake_main)

    assert app.main(["ports", "show"]) == 7
    assert seen["argv"] == ["show"]


def test_a_failed_declaration_write_leaves_that_project_without_a_lease(
    tmp_path: Path, monkeypatch
):
    # A project whose files cannot all be written must end the run holding no
    # lease: a lease nothing can honour is worse than no lease at all. Its
    # neighbours -- both the ones written before it and the ones after -- must
    # be served completely.
    alpha = make_project(tmp_path, "alpha", 8080)
    beta = make_project(tmp_path, "beta", 8500)
    zulu = make_project(tmp_path, "zulu", 8600)
    ledger_path = tmp_path / "services.toml"

    real_write_assigned = cli.write_assigned

    def failing(path: Path, port_name: str, port: int) -> None:
        if path.parent.name == "beta":
            raise OSError("no space left on device")
        real_write_assigned(path, port_name, port)

    monkeypatch.setattr(cli, "write_assigned", failing)

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 2
    assert "beta" in output
    # beta holds no lease, and its neighbours hold theirs.
    assert {lease.project: lease.port for lease in load_leases(ledger_path)} == {
        "alpha": 8080,
        "zulu": 8600,
    }
    for project, port in ((alpha, 8080), (zulu, 8600)):
        assert load_declaration(project / ".harbor.toml").ports[0].assigned == port
        assert f"HARBOR_PORT_WEB={port}" in (project / ".env").read_text(encoding="utf-8")
        assert (project / "HARBOR_PORTS.md").is_file()
    assert load_declaration(beta / ".harbor.toml").ports[0].assigned is None
    assert not (beta / ".env").exists()
    # `HARBOR_PORTS.md` is written first and may well have landed. That is
    # harmless and deliberate: it is identical in every project and names no
    # port, so it can contradict neither the ledger nor `.env`. What must not
    # exist for a withdrawn project is anything carrying a number.
    # What did land is reported, not hidden behind the error line.
    assert "alpha" in output
    assert "zulu" in output


def test_a_failed_explainer_write_withdraws_only_that_projects_lease(tmp_path: Path):
    # The same guarantee, driven by a real filesystem refusal rather than a
    # stub: `HARBOR_PORTS.md` is a directory, so writing it cannot succeed.
    alpha = make_project(tmp_path, "alpha", 8080)
    beta = make_project(tmp_path, "beta", 8500)
    zulu = make_project(tmp_path, "zulu", 8600)
    (beta / "HARBOR_PORTS.md").mkdir()
    ledger_path = tmp_path / "services.toml"

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 2
    assert "beta" in output
    assert {lease.project: lease.port for lease in load_leases(ledger_path)} == {
        "alpha": 8080,
        "zulu": 8600,
    }
    # The withdrawal above says 8500 belongs to nobody. Nothing of beta's may
    # contradict it: an `.env` publishing 8500 would make beta bind a port the
    # allocator believes free, and the next run would hand it to somebody else.
    assert not (beta / ".env").exists()
    assert load_declaration(beta / ".harbor.toml").ports[0].assigned is None
    for project, port in ((alpha, 8080), (zulu, 8600)):
        assert load_declaration(project / ".harbor.toml").ports[0].assigned == port
        assert f"HARBOR_PORT_WEB={port}" in (project / ".env").read_text(encoding="utf-8")
        assert (project / "HARBOR_PORTS.md").is_file()


def test_an_env_that_is_not_utf8_is_reported_not_a_traceback(tmp_path: Path):
    # Somebody else's repository may hold a cp1252 password in `.env`. That is
    # not a crash: the project is named, dropped whole, and its bytes are left
    # exactly as they were.
    alpha = make_project(tmp_path, "alpha", 8080)
    (alpha / ".env").write_bytes(b"PASSWORD=caf\xff\n")
    beta = make_project(tmp_path, "beta", 8500)
    ledger_path = tmp_path / "services.toml"

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 2
    assert "alpha" in output
    assert ".env" in output
    assert (alpha / ".env").read_bytes() == b"PASSWORD=caf\xff\n"
    assert load_declaration(alpha / ".harbor.toml").ports[0].assigned is None
    assert {lease.project: lease.port for lease in load_leases(ledger_path)} == {"beta": 8500}
    assert load_declaration(beta / ".harbor.toml").ports[0].assigned == 8500


def test_an_unreadable_env_is_reported_not_a_traceback(tmp_path: Path):
    # `.env` exists but cannot be read (here: it is a directory). Same contract
    # as a corrupted fence -- named, dropped whole, non-zero exit, no traceback.
    alpha = make_project(tmp_path, "alpha", 8080)
    (alpha / ".env").mkdir()
    beta = make_project(tmp_path, "beta", 8500)
    ledger_path = tmp_path / "services.toml"

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 2
    assert "alpha" in output
    assert ".env" in output
    assert load_declaration(alpha / ".harbor.toml").ports[0].assigned is None
    assert {lease.project: lease.port for lease in load_leases(ledger_path)} == {"beta": 8500}
    assert load_declaration(beta / ".harbor.toml").ports[0].assigned == 8500


def _widened_onto_an_incumbent(root: Path, ledger_path: Path, extra: str = "") -> Path:
    """Set up the one situation in which a *lease-holder* is told to move.

    The incumbent is never renumbered, so a project can only be reassigned off a
    port it actually holds by widening its own address onto a senior lease:
    imageharbor holds 192.168.1.5:8080 and now asks to bind 0.0.0.0:8080, which
    gte has held at 127.0.0.1:8080 since longer ago. Two specific addresses do
    not contend, so both leases are legal in the ledger; the widening is what
    makes them contend, and the junior is the one that moves.

    Returns imageharbor's directory. `extra` is appended to its declaration.
    """
    save_leases(
        ledger_path,
        [
            Lease("gte", "web", "hpz440", "127.0.0.1", 8080, date(2026, 7, 5)),
            Lease("imageharbor", "web", "hpz440", "192.168.1.5", 8080, date(2026, 8, 1)),
        ],
    )
    incumbent = root / "gte"
    incumbent.mkdir()
    (incumbent / ".harbor.toml").write_text(
        'project = "gte"\nhost = "hpz440"\n\n[[port]]\n'
        'name = "web"\nwant = 8080\nassigned = 8080\naddr = "127.0.0.1"\n',
        encoding="utf-8",
    )
    moved = root / "imageharbor"
    moved.mkdir()
    (moved / ".harbor.toml").write_text(
        'project = "imageharbor"\nhost = "hpz440"\n\n[[port]]\n'
        'name = "web"\nwant = 8080\nassigned = 8080\naddr = "0.0.0.0"\n' + extra,
        encoding="utf-8",
    )
    return moved


def test_new_only_keeps_the_withheld_ports_current_number_in_env(tmp_path: Path):
    # The fence is rewritten wholesale, so a project holding one withheld port
    # and one granted port is where a mistake shows: the withheld variable must
    # keep the number it currently holds, never the move that was refused.
    ledger_path = tmp_path / "services.toml"
    moved = _widened_onto_an_incumbent(
        tmp_path, ledger_path, extra='\n[[port]]\nname = "api"\nwant = 8600\n'
    )
    (moved / ".env").write_text(
        f"SECRET=keepme\n{FENCE_START}\nHARBOR_PORT_WEB=8080\n{FENCE_END}\n",
        encoding="utf-8",
    )

    code, output = run(["sync", "--new-only"], tmp_path, ledger_path)

    assert code == 1
    assert "withheld" in output.lower()
    env = (moved / ".env").read_text(encoding="utf-8")
    assert "HARBOR_PORT_WEB=8080" in env  # withheld: the number it already has
    assert "HARBOR_PORT_API=8600" in env  # granted: the new one
    assert "8100" not in env  # the refused reassignment never leaks in
    assert "SECRET=keepme" in env  # content outside the fence survives
    assert load_declaration(moved / ".harbor.toml").ports[0].assigned == 8080
    held = {
        (lease.project, lease.name): (lease.addr, lease.port)
        for lease in load_leases(ledger_path)
    }
    assert held == {
        ("gte", "web"): ("127.0.0.1", 8080),
        ("imageharbor", "web"): ("192.168.1.5", 8080),  # the withheld lease stands
        ("imageharbor", "api"): ("0.0.0.0", 8600),
    }


def test_ports_url_is_accepted_before_and_after_the_subcommand(tmp_path: Path):
    # argparse's usual trap: a subparser default overwriting a value given
    # before the subcommand. Both orderings must reach the same place.
    url = "http://example.invalid:9/ports.json"

    assert cli._parser().parse_args(["--ports-url", url, "scan"]).ports_url == url
    assert cli._parser().parse_args(["scan", "--ports-url", url]).ports_url == url
    assert cli._parser().parse_args(["scan"]).ports_url == cli.PORTS_URL_DEFAULT

    make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"
    before = run(["--ports-url", url, "scan"], tmp_path, ledger_path)
    after = run(["scan", "--ports-url", url], tmp_path, ledger_path)

    assert before == after
    assert before[0] == 1


def test_env_is_written_last_so_a_failure_never_publishes_a_withdrawn_port(
    tmp_path: Path, monkeypatch
):
    # A project that fails has this run's decisions withdrawn from the ledger.
    # That withdrawal is only truthful if nothing of the project's is left
    # publishing the port: an `.env` naming 8080 while the ledger records 8080
    # as free is two projects on one port waiting to happen -- this tool's
    # founding failure, reached through the mechanism meant to prevent it. It
    # does not heal itself either: only a lease reserves a port, and `allocate`
    # gives a bare `assigned` no claim at all.
    alpha = make_project(tmp_path, "alpha", 8080)
    (alpha / ".env").write_text("SECRET=keepme\n", encoding="utf-8")
    ledger_path = tmp_path / "services.toml"

    def failing(path: Path) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(cli.explainer, "write_explainer", failing)

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 2
    assert "alpha" in output
    env = (alpha / ".env").read_text(encoding="utf-8")
    assert "HARBOR_PORT_WEB" not in env  # nothing publishes the withdrawn port
    assert "SECRET=keepme" in env  # and the secrets are still there
    assert load_leases(ledger_path) == []
    assert load_declaration(alpha / ".harbor.toml").ports[0].assigned is None
    assert "withdrawn from the ledger" in output


def test_a_failed_reassignment_says_the_project_keeps_the_lease_it_already_had(
    tmp_path: Path,
):
    # Withdrawing a run's decisions does not withdraw the lease the project
    # walked in with: `apply_decisions` folds from the original ledger. A
    # project being *reassigned* therefore ends a failed run still holding its
    # old port, and the message must say so -- telling an operator to hunt for
    # a missing lease that is sitting right there is worse than saying nothing.
    ledger_path = tmp_path / "services.toml"
    save_leases(
        ledger_path,
        [
            Lease("gte", "web", "hpz440", "100.69.239.123", 8080, date(2026, 7, 5)),
            Lease("beta", "web", "hpz440", "127.0.0.1", 8080, date(2026, 8, 9)),
        ],
    )
    gte = tmp_path / "gte"
    gte.mkdir()
    (gte / ".harbor.toml").write_text(
        'project = "gte"\nhost = "hpz440"\n\n[[port]]\nname = "web"\n'
        'want = 8080\nassigned = 8080\naddr = "100.69.239.123"\n',
        encoding="utf-8",
    )
    # beta wants to widen to every address, which collides with gte's lease.
    # beta is the junior, so it is the one that moves -- to 8100.
    beta = tmp_path / "beta"
    beta.mkdir()
    (beta / ".harbor.toml").write_text(
        'project = "beta"\nhost = "hpz440"\n\n[[port]]\nname = "web"\n'
        'want = 8080\nassigned = 8080\naddr = "0.0.0.0"\n',
        encoding="utf-8",
    )
    (beta / "HARBOR_PORTS.md").mkdir()  # so writing beta cannot succeed

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 2
    held = {(lease.project, lease.addr, lease.port) for lease in load_leases(ledger_path)}
    assert held == {
        ("gte", "100.69.239.123", 8080),
        ("beta", "127.0.0.1", 8080),  # the lease beta walked in with, still there
    }
    assert "withdrawn from the ledger" in output
    assert "before the run" in output
    assert "holding no lease" not in output  # it is holding one


def test_a_degraded_run_reports_drift_against_the_held_port_not_the_refused_one(
    tmp_path: Path,
):
    # This path refuses to grant anything it cannot verify. A drift warning
    # naming the port it just refused would tell the operator to change another
    # repository's compose default to a number nobody was granted -- a guess, on
    # the one path whose whole contract is that it does not guess.
    ledger_path = tmp_path / "services.toml"
    moved = _widened_onto_an_incumbent(tmp_path, ledger_path)
    (moved / "docker-compose.yml").write_text(
        'services:\n  a:\n    ports:\n      - "${HARBOR_PORT_WEB:-9999}:80"\n',
        encoding="utf-8",
    )

    code, output = run(["sync"], tmp_path, ledger_path, state=live(complete=False))

    assert code == 1
    assert "incomplete" in output.lower()
    assert "9999" in output  # the drift is still reported
    assert "assigned 8080" in output  # against the number it is actually on
    assert "8100" not in output  # never against the grant that was refused
    assert not (moved / ".env").exists()
    assert {
        (lease.project, lease.addr): lease.port for lease in load_leases(ledger_path)
    } == {("gte", "127.0.0.1"): 8080, ("imageharbor", "192.168.1.5"): 8080}


def test_a_compose_file_that_is_not_utf8_is_skipped_not_a_traceback(tmp_path: Path):
    # Another repository's compose file may hold any bytes at all. It is read
    # only to warn about drift, so an undecodable one costs a warning, not the
    # run -- the same class of fault already fixed for `.env`, one call away.
    alpha = make_project(tmp_path, "alpha", 8080)
    (alpha / "docker-compose.yml").write_bytes(
        b'services:\n  a:\n    # caf\xff\n    ports:\n'
        b'      - "${HARBOR_PORT_WEB:-9999}:80"\n'
    )
    ledger_path = tmp_path / "services.toml"

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 0
    assert "9999" not in output
    assert {lease.project: lease.port for lease in load_leases(ledger_path)} == {"alpha": 8080}
    assert "HARBOR_PORT_WEB=8080" in (alpha / ".env").read_text(encoding="utf-8")


def _freeze_mtimes(root: Path, stamp: int = 1_000_000) -> dict[Path, int]:
    """Set every file's mtime under `root` to `stamp` and return the mapping.

    A run that rewrites a file identically is invisible to a content check but
    not to this one, and comparing against a stamp set by hand is immune to the
    clock resolution a same-millisecond rewrite would hide behind.
    """
    stamps: dict[Path, int] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            os.utime(path, (stamp, stamp))
            stamps[path] = path.stat().st_mtime_ns
    return stamps


def test_sync_restores_a_deleted_env(tmp_path: Path):
    # `.env` is gitignored, so *every* fresh clone starts without one. A project
    # in steady state yields only `keep` decisions; if those keep it out of the
    # write pass, sync says "up to date" over a project whose compose file is
    # about to interpolate its default -- a port the ledger has leased to
    # somebody else. That is this tool's founding failure, at exit 0.
    project = make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"
    run(["sync"], tmp_path, ledger_path)
    (project / ".env").unlink()

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 0
    assert "HARBOR_PORT_WEB=8080" in (project / ".env").read_text(encoding="utf-8")
    assert "repaired" in output.lower()
    assert "up to date" not in output
    # A repair restores what is already leased; it grants nothing.
    assert {lease.project: lease.port for lease in load_leases(ledger_path)} == {"alpha": 8080}


def test_sync_repairs_a_hand_edited_fence(tmp_path: Path):
    # Same fault, reached by editing rather than deleting: the fence names a
    # port the ledger never granted, and every `keep` decision hides it.
    project = make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"
    run(["sync"], tmp_path, ledger_path)
    (project / ".env").write_text(
        f"SECRET=keepme\n{FENCE_START}\nHARBOR_PORT_WEB=9999\n{FENCE_END}\n",
        encoding="utf-8",
    )

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 0
    env = (project / ".env").read_text(encoding="utf-8")
    assert "HARBOR_PORT_WEB=8080" in env
    assert "9999" not in env
    assert "SECRET=keepme" in env  # a repair is still only the fence
    assert "repaired" in output.lower()


def test_sync_repairs_a_missing_explainer(tmp_path: Path):
    # `HARBOR_PORTS.md` is the only thing telling the next person why a port is
    # fenced into `.env`. It must come back too, not only when a port moves.
    project = make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"
    run(["sync"], tmp_path, ledger_path)
    (project / "HARBOR_PORTS.md").unlink()

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 0
    assert (project / "HARBOR_PORTS.md").is_file()
    assert "repaired" in output.lower()


def test_scan_reports_a_pending_repair_and_writes_nothing(tmp_path: Path):
    project = make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"
    run(["sync"], tmp_path, ledger_path)
    (project / ".env").unlink()

    code, output = run(["scan"], tmp_path, ledger_path)

    assert code == 1
    assert "alpha" in output
    assert "repair" in output.lower()
    assert "up to date" not in output
    assert not (project / ".env").exists()


def test_sync_writes_nothing_when_everything_already_matches(tmp_path: Path):
    # The other half of the contract: repairing what has drifted must not turn
    # sync into a run that rewrites other people's repositories every time.
    make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"
    run(["sync"], tmp_path, ledger_path)
    before = _freeze_mtimes(tmp_path)

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 0
    assert output == "up to date\n"
    after = {path: path.stat().st_mtime_ns for path in before}
    assert after == before


def test_two_directories_declaring_one_project_is_a_hard_error(tmp_path: Path):
    # `.harbor.toml` travels with a copy, so a `-backup` directory or a second
    # worktree declares the same project twice. Keying declarations by project
    # name silently drops one of them: the lease is written for one directory
    # and the files for the other.
    gte = make_project(tmp_path, "gte", 8080)
    backup = tmp_path / "gte-backup"
    backup.mkdir()
    (backup / ".harbor.toml").write_text(
        'project = "gte"\nhost = "hpz440"\n\n[[port]]\nname = "web"\nwant = 8080\n',
        encoding="utf-8",
    )
    ledger_path = tmp_path / "services.toml"

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 2
    assert "gte-backup" in output  # both paths are named, not just the winner
    assert str(gte / ".harbor.toml") in output
    assert not ledger_path.exists()
    assert not (gte / ".env").exists()
    assert not (backup / ".env").exists()
    assert not (gte / "HARBOR_PORTS.md").exists()


def test_scan_says_its_grants_are_unverified_when_live_state_is_incomplete(tmp_path: Path):
    # `sync` refuses to grant on this input. `scan` printing "would write
    # alpha/web = 8080" with no caveat promises something the next command will
    # not do -- and the caveat `main()` prints never reaches an injected-state
    # caller at all.
    make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"

    code, output = run(["scan"], tmp_path, ledger_path, state=live(complete=False))

    assert code == 1
    assert "8080" in output
    assert "unverified" in output.lower()
    assert "incomplete" in output.lower()


def test_a_project_name_that_would_corrupt_the_ledger_is_refused(tmp_path: Path):
    # The project name comes from a `.harbor.toml` in a repository this tool
    # does not own, and it is interpolated into the ledger's TOML strings. A
    # quote in it wrote a ledger that no later command could load -- the tool
    # could not start again until somebody hand-edited `services.toml`.
    project = tmp_path / "evil"
    project.mkdir()
    (project / ".harbor.toml").write_text(
        'project = \'ev"il\'\nhost = "hpz440"\n\n[[port]]\nname = "web"\nwant = 8080\n',
        encoding="utf-8",
    )
    ledger_path = tmp_path / "services.toml"

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 2
    assert "project" in output
    assert not ledger_path.exists()
    assert not (project / ".env").exists()
    # And every later command still works, which is the whole point.
    assert run(["show"], tmp_path, ledger_path)[0] == 0


def _stale_project(root: Path, name: str, want: int, assigned: int) -> Path:
    """A committed `.harbor.toml` naming an `assigned` its project has no lease on.

    The ordinary shape of a fresh clone that once shared a tree with somebody
    else: `assigned` travels in git, the lease does not, and `.env` is
    gitignored so it is simply absent.
    """
    project = root / name
    project.mkdir()
    (project / ".harbor.toml").write_text(
        f'project = "{name}"\nhost = "hpz440"\n\n[[port]]\n'
        f"name = \"web\"\nwant = {want}\nassigned = {assigned}\n",
        encoding="utf-8",
    )
    return project


def _repair_targets(output: str) -> set[str]:
    """The projects a report says it repaired, or would repair."""
    targets: set[str] = set()
    for line in output.splitlines():
        words = line.partition(":")[0].split()
        if words[:1] == ["repaired"] or words[:2] == ["would", "repair"]:
            targets.add(words[-1])
    return targets


def test_new_only_never_publishes_an_incumbents_port_into_a_withheld_project(
    tmp_path: Path,
):
    # Only a lease reserves a port; an `assigned` in a file this tool does not
    # own does not. beta's committed declaration still claims 8080, but alpha
    # holds the lease on it. Treating that stale number as "the port beta is
    # already on" published 8080 into beta's `.env` -- two projects on one port,
    # created by the repair mechanism, on the path the scheduled timer runs, and
    # reported as a repair.
    ledger_path = tmp_path / "services.toml"
    alpha = make_project(tmp_path, "alpha", 8080)
    run(["sync"], tmp_path, ledger_path)
    beta = _stale_project(tmp_path, "beta", 8080, 8080)

    code, output = run(["sync", "--new-only"], tmp_path, ledger_path)

    assert code == 1
    assert "withheld" in output.lower()
    # Nothing this run was entitled to publish for beta, so nothing was.
    assert not (beta / ".env").exists()
    assert not (beta / "HARBOR_PORTS.md").exists()
    assert "beta" not in _repair_targets(output)
    # And the incumbent still holds 8080, alone.
    assert {lease.project: lease.port for lease in load_leases(ledger_path)} == {"alpha": 8080}
    assert "HARBOR_PORT_WEB=8080" in (alpha / ".env").read_text(encoding="utf-8")


def test_scan_predicts_exactly_what_new_only_repairs(tmp_path: Path):
    # `scan` is the dry run for `sync`. It filtered repairs by every project
    # with a change; `sync --new-only` filtered only by the changes it applied,
    # so a withheld project appeared as a repair in one and not the other.
    ledger_path = tmp_path / "services.toml"
    make_project(tmp_path, "alpha", 8080)
    gamma = make_project(tmp_path, "gamma", 8500)
    run(["sync"], tmp_path, ledger_path)
    (gamma / ".env").unlink()  # the fresh clone: gitignored, so simply absent
    beta = _stale_project(tmp_path, "beta", 8080, 8080)

    scan_code, scan_output = run(["scan"], tmp_path, ledger_path)
    sync_code, sync_output = run(["sync", "--new-only"], tmp_path, ledger_path)

    assert scan_code == 1
    assert sync_code == 1
    assert _repair_targets(scan_output) == _repair_targets(sync_output) == {"gamma"}
    assert "HARBOR_PORT_WEB=8500" in (gamma / ".env").read_text(encoding="utf-8")
    assert not (beta / ".env").exists()


def test_a_repair_of_the_explainer_alone_does_not_touch_env(tmp_path: Path):
    # Bumping TEMPLATE_VERSION puts every participating project into the repair
    # set at once. Rewriting each one's `.env` byte-for-byte identically to move
    # its mtime is churn in somebody else's repository for no reason.
    project = make_project(tmp_path, "alpha", 8080)
    ledger_path = tmp_path / "services.toml"
    run(["sync"], tmp_path, ledger_path)
    (project / "HARBOR_PORTS.md").write_text(
        "harbor-console-template-version: 1\n", encoding="utf-8"
    )
    before = _freeze_mtimes(tmp_path)

    code, output = run(["sync"], tmp_path, ledger_path)

    assert code == 0
    assert "repaired" in output.lower()
    assert "harbor-console-template-version: 2" in (
        project / "HARBOR_PORTS.md"
    ).read_text(encoding="utf-8")
    unchanged = {path: stamp for path, stamp in before.items() if path.name != "HARBOR_PORTS.md"}
    after = {path: path.stat().st_mtime_ns for path in unchanged}
    assert after == unchanged
