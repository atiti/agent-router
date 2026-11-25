from __future__ import annotations

import json
import plistlib
from types import SimpleNamespace

from typer.testing import CliRunner

from agentroute import maintenance_cli as cli


def test_install_creates_scoped_login_schedule_and_is_idempotent(tmp_path, monkeypatch):
    target = tmp_path / "LaunchAgents" / "maintenance.plist"
    monkeypatch.setattr(cli, "service_path", lambda: target)
    monkeypatch.setattr(cli, "agentroute_home", lambda: tmp_path / "runtime")
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli, "_agentroute_executable", lambda: tmp_path / "agentroute")
    calls = []

    def launchctl(*args):
        calls.append(args)
        return SimpleNamespace(returncode=1 if args[0] == "print" else 0, stderr="", stdout="")

    monkeypatch.setattr(cli, "_launchctl", launchctl)
    runner = CliRunner()
    assert runner.invoke(cli.maintenance_app, ["install"]).exit_code == 0
    data = plistlib.loads(target.read_bytes())
    assert data["ProgramArguments"][1:] == ["maintenance", "run", "--apply", "--summary"]
    assert data["RunAtLoad"] is True and data["StartInterval"] == 60
    assert set(data["EnvironmentVariables"]) == {"PATH"}
    assert str(tmp_path / "runtime" / "bin") in data["EnvironmentVariables"]["PATH"]
    assert runner.invoke(cli.maintenance_app, ["install"]).exit_code == 0
    assert sum(args[0] == "bootstrap" for args in calls) == 2


def test_install_preserves_unrelated_or_symlinked_service(tmp_path, monkeypatch):
    target = tmp_path / "maintenance.plist"
    target.write_bytes(plistlib.dumps({"Label": "unrelated"}))
    original = target.read_bytes()
    monkeypatch.setattr(cli, "service_path", lambda: target)
    monkeypatch.setattr(cli, "agentroute_home", lambda: tmp_path)
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli, "_agentroute_executable", lambda: tmp_path / "agentroute")
    result = CliRunner().invoke(cli.maintenance_app, ["install"])
    assert result.exit_code != 0 and target.read_bytes() == original
    target.unlink()
    target.symlink_to(tmp_path / "elsewhere")
    assert CliRunner().invoke(cli.maintenance_app, ["install"]).exit_code != 0


def test_run_defaults_to_inspection_and_status_surfaces_last_tick(tmp_path, monkeypatch):
    from agentroute import maintenance

    calls = []
    result_file = tmp_path / "status.json"
    result_file.write_text(json.dumps({"checked_at": 100, "errors": []}))
    monkeypatch.setattr(
        maintenance, "run_once", lambda *, apply: calls.append(apply) or {"ok": True}
    )
    monkeypatch.setattr(maintenance, "status_path", lambda: result_file)
    monkeypatch.setattr(cli, "service_path", lambda: tmp_path / "missing.plist")
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli, "_launchctl", lambda *args: SimpleNamespace(returncode=0))
    runner = CliRunner()
    assert runner.invoke(cli.maintenance_app, ["run"]).exit_code == 0
    assert runner.invoke(cli.maintenance_app, ["run", "--apply"]).exit_code == 0
    assert calls == [False, True]
    status = json.loads(runner.invoke(cli.maintenance_app, ["status"]).output)
    assert status["schedule_loaded"] is True
    assert status["schedule_installed"] is False
    assert status["last_run"]["checked_at"] == 100
