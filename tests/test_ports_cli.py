import io
from datetime import date
from pathlib import Path

from harbor_console import app
from harbor_console.ports import cli
from harbor_console.ports.declaration import load_declaration
from harbor_console.ports.envfile import FENCE_START
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
