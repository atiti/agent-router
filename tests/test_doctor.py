import json
import plistlib

from agentroute.bridge_service import BridgeHealth
from agentroute.config import default_config, save_config
from agentroute.doctor import EXPECTED_RUNTIME_REVISION, _desktop_check, run_doctor
from agentroute.install import hook_command
from agentroute.providers import ensure_claude_bridge_backend


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
            "Interrupt": [{"hooks": [{"command": hook_command("interrupt")}]}],
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
            "Interrupt": [{"hooks": [{"command": hook_command("interrupt")}]}],
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


def _doctor_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path / ".agentroute"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setattr("agentroute.doctor.Path.home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("agentroute.bridge_service.Path.home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("agentroute.doctor.platform.system", lambda: "Linux")
    return default_config()


def _config_with_claude_bridge(tmp_path, monkeypatch):
    config = _doctor_env(tmp_path, monkeypatch)
    ensure_claude_bridge_backend(config, 8090)
    return config


def test_doctor_fails_when_a_claude_backend_has_no_bridge_service(tmp_path, monkeypatch):
    config = _config_with_claude_bridge(tmp_path, monkeypatch)
    assert "claude" in config.backends

    checks = {check.name: check for check in run_doctor(config)}

    assert checks["bridge"].status == "fail"
    assert "agentroute bridge install" in checks["bridge"].detail


def test_doctor_passes_when_the_bridge_answers(tmp_path, monkeypatch):
    config = _config_with_claude_bridge(tmp_path, monkeypatch)
    monkeypatch.setattr("agentroute.doctor.service_installed", lambda: True)
    monkeypatch.setattr("agentroute.doctor.service_port", lambda: 8090)
    monkeypatch.setattr(
        "agentroute.doctor.probe_health",
        lambda port: BridgeHealth("healthy", "responds on loopback"),
    )

    checks = {check.name: check for check in run_doctor(config)}

    assert checks["bridge"].status == "pass"
    assert "8090" in checks["bridge"].detail


def test_doctor_fails_when_the_bridge_is_installed_but_silent(tmp_path, monkeypatch):
    config = _config_with_claude_bridge(tmp_path, monkeypatch)
    monkeypatch.setattr("agentroute.doctor.service_installed", lambda: True)
    monkeypatch.setattr("agentroute.doctor.service_port", lambda: 8090)
    monkeypatch.setattr(
        "agentroute.doctor.probe_health",
        lambda port: BridgeHealth("unreachable", "no answer"),
    )

    checks = {check.name: check for check in run_doctor(config)}

    assert checks["bridge"].status == "fail"
    assert "not answering" in checks["bridge"].detail


def test_doctor_skips_the_bridge_when_no_claude_backend_is_configured(tmp_path, monkeypatch):
    config = _doctor_env(tmp_path, monkeypatch)

    checks = {check.name: check for check in run_doctor(config)}

    assert checks["bridge"].status == "pass"
    assert "not configured" in checks["bridge"].detail


def test_ensure_claude_bridge_backend_registers_tiers_and_is_idempotent(tmp_path, monkeypatch):
    config = _doctor_env(tmp_path, monkeypatch)

    assert ensure_claude_bridge_backend(config, 8090) is True
    backend = config.backends["claude"]
    assert backend.enabled is True
    assert backend.base_url == "http://127.0.0.1:8090/v1"
    assert backend.codex_provider == "agentroute-claude"
    assert backend.tiers["max"].model == "claude-opus-5-5"
    assert backend.review_model == "claude-haiku-4-5-20251001"

    # Re-running must not duplicate or discard the backend.
    config.backends["claude"].tiers["fast"].model = "claude-sonnet-5"
    assert ensure_claude_bridge_backend(config, 8091) is False
    assert config.backends["claude"].tiers["fast"].model == "claude-sonnet-5"
    assert config.backends["claude"].base_url == "http://127.0.0.1:8091/v1"
