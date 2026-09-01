from __future__ import annotations

import io
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
    assert not (beta / "HARBOR_PORTS.md").exists()
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


def test_new_only_keeps_the_withheld_ports_current_number_in_env(tmp_path: Path):
    # The fence is rewritten wholesale, so a project holding one withheld port
    # and one granted port is where a mistake shows: the withheld variable must
    # keep the number it currently holds, never the move that was refused.
    ledger_path = tmp_path / "services.toml"
    save_leases(ledger_path, [Lease("gte", "web", "hpz440", "0.0.0.0", 8080, date(2026, 7, 5))])
    make_project(tmp_path, "gte", 8080)
    moved = tmp_path / "imageharbor"
    moved.mkdir()
    (moved / ".harbor.toml").write_text(
        'project = "imageharbor"\nhost = "hpz440"\n\n'
        '[[port]]\nname = "web"\nwant = 8080\nassigned = 8080\n\n'
        '[[port]]\nname = "api"\nwant = 8600\n',
        encoding="utf-8",
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
    held = {(lease.project, lease.name): lease.port for lease in load_leases(ledger_path)}
    assert held == {("gte", "web"): 8080, ("imageharbor", "api"): 8600}


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
