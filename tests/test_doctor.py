import json
import plistlib

from agentroute.config import default_config, save_config
from agentroute.doctor import EXPECTED_RUNTIME_REVISION, _desktop_check, run_doctor
from agentroute.install import hook_command


def test_doctor_validates_local_runtime_hooks_and_audit(tmp_path, monkeypatch):
    home = tmp_path / ".agentroute"
    codex_home = tmp_path / ".codex"
    (home / "bin").mkdir(parents=True)
    codex_home.mkdir()
    binary = home / "bin" / "codex-bin"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    (home / "build-id").write_text(f"commit-{EXPECTED_RUNTIME_REVISION}\n", encoding="utf-8")
    monkeypatch.setenv("AGENTROUTE_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr("agentroute.doctor.Path.home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("agentroute.doctor.platform.system", lambda: "Linux")
    config = default_config()
    config.enabled = True
    save_config(config)
    hooks = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"command": hook_command("user-prompt-submit")}]}
            ],
            "Stop": [{"hooks": [{"command": hook_command("stop")}]}],
            "SubagentStop": [{"hooks": [{"command": hook_command("stop")}]}],
        }
    }
    (codex_home / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")

    checks = run_doctor(config)
    by_name = {check.name: check for check in checks}

    assert by_name["runtime"].status == "pass"
    assert by_name["hooks"].status == "pass"
    assert by_name["audit-db"].status == "pass"
    assert not [check for check in checks if check.status == "fail"]


def test_doctor_requires_commands_under_each_exact_hook_event(tmp_path, monkeypatch):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr("agentroute.doctor.Path.home", classmethod(lambda cls: tmp_path))
    hooks = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {"command": hook_command("user-prompt-submit")},
                        {"command": hook_command("stop")},
                    ]
                }
            ],
            "Stop": [{"hooks": [{"command": hook_command("stop")}]}],
        }
    }
    (codex_home / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")

    from agentroute.doctor import _hooks_check

    check = _hooks_check()

    assert check.status == "fail"
    assert check.detail == "missing events: SubagentStop"


def test_doctor_warns_when_desktop_embeds_an_old_runtime(tmp_path, monkeypatch):
    destination = tmp_path / "ChatGPT-Routed.app"
    resources = destination / "Contents" / "Resources"
    resources.mkdir(parents=True)
    (resources / "codex-bin").write_text("old", encoding="utf-8")
    with (destination / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump({"AgentRouteDesktopBuild": "provider-routing-v21"}, handle)

    check = _desktop_check(destination, "provider-routing-v26")

    assert check.status == "warn"
    assert "provider-routing-v21" in check.detail
    assert "provider-routing-v26" in check.detail
