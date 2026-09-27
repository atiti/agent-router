import plistlib
import subprocess

import pytest

from agentroute import bridge_service
from agentroute.bridge_service import (
    SERVICE_LABEL,
    BridgeHealth,
    BridgeServiceError,
    install_service,
    probe_health,
    service_installed,
    service_port,
    uninstall_service,
    write_service_unit,
)


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr("agentroute.bridge_service.Path.home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "agentroute.bridge_service._agentroute_executable",
        lambda: tmp_path / "venv" / "bin" / "agentroute",
    )
    monkeypatch.setattr(
        "agentroute.bridge_service.agentroute_home", lambda: tmp_path / ".agentroute"
    )


def test_launch_agent_unit_has_the_expected_arguments(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("agentroute.bridge_service.platform.system", lambda: "Darwin")

    path = write_service_unit(8090, "claude-code", "127.0.0.1")

    with path.open("rb") as handle:
        payload = plistlib.load(handle)
    assert payload["Label"] == SERVICE_LABEL
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    arguments = payload["ProgramArguments"]
    assert arguments[1:3] == ["bridge", "serve"]
    assert "--credential" in arguments and "claude-code" in arguments
    assert arguments[arguments.index("--port") + 1] == "8090"
    assert payload["StandardErrorPath"].endswith("bridge.log")
    assert service_port() == 8090


def test_systemd_unit_uses_the_same_executable_and_port(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("agentroute.bridge_service.platform.system", lambda: "Linux")

    path = write_service_unit(8123, "api-key", "127.0.0.1")

    text = path.read_text(encoding="utf-8")
    assert "ExecStart=" in text
    assert "--credential api-key" in text
    assert "--port 8123" in text
    assert "WantedBy=default.target" in text
    assert path.name == "agentroute-claude-bridge.service"
    assert service_port() == 8123


def test_install_writes_loads_and_health_checks(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("agentroute.bridge_service.platform.system", lambda: "Darwin")
    loaded: list = []
    monkeypatch.setattr(
        "agentroute.bridge_service._load_service", lambda path: loaded.append(path)
    )
    monkeypatch.setattr(
        "agentroute.bridge_service.probe_health",
        lambda port, host: BridgeHealth("healthy", "responds on loopback"),
    )

    path, detail = install_service()

    assert loaded == [path]
    assert path.exists() and service_installed()
    assert detail == "responds on loopback"


def test_install_fails_when_the_service_never_answers(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("agentroute.bridge_service.platform.system", lambda: "Darwin")
    monkeypatch.setattr("agentroute.bridge_service._load_service", lambda path: None)
    monkeypatch.setattr(
        "agentroute.bridge_service.probe_health",
        lambda port, host: BridgeHealth("unreachable", "no answer"),
    )

    with pytest.raises(BridgeServiceError, match="healthz"):
        install_service(timeout=0.01)


def test_uninstall_removes_the_definition(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("agentroute.bridge_service.platform.system", lambda: "Darwin")
    monkeypatch.setattr("agentroute.bridge_service._unload_service", lambda: None)
    path = write_service_unit(8090, "claude-code", "127.0.0.1")

    removed, existed = uninstall_service()

    assert removed == path and existed is True
    assert not path.exists()
    assert not service_installed()


def test_probe_health_reports_unreachable_without_raising(monkeypatch):
    def boom(url, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("agentroute.bridge_service.urllib.request.urlopen", boom)
    health = probe_health(59999)
    assert health.status == "unreachable"
    assert "59999" in health.detail


def test_service_port_is_none_without_a_unit(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("agentroute.bridge_service.platform.system", lambda: "Darwin")
    assert service_port() is None
    assert bridge_service.SERVICE_LABEL == SERVICE_LABEL


def test_bootstrap_retries_the_launchd_teardown_race(tmp_path, monkeypatch):
    """`bootout` is asynchronous; a bootstrap landing in the window returns EIO 5."""
    attempts: list[list[str]] = []
    slept: list[float] = []
    state = {"bootstrapped": False}

    def fake_run(command):
        attempts.append(command)
        if command[:2] == ["launchctl", "print"]:
            # Nothing is loaded until a bootstrap lands.
            return subprocess.CompletedProcess(command, 0 if state["bootstrapped"] else 113, "", "")
        if command[:2] == ["launchctl", "bootstrap"]:
            first = len([c for c in attempts if c[:2] == ["launchctl", "bootstrap"]]) == 1
            if not first:
                state["bootstrapped"] = True
            return subprocess.CompletedProcess(
                command,
                5 if first else 0,
                "",
                "Bootstrap failed: 5: Input/output error" if first else "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("agentroute.bridge_service._run", fake_run)
    monkeypatch.setattr("agentroute.bridge_service.platform.system", lambda: "Darwin")
    monkeypatch.setattr("agentroute.bridge_service.time.sleep", slept.append)

    bridge_service._load_service(tmp_path / "unit.plist")

    bootstraps = [c for c in attempts if c[:2] == ["launchctl", "bootstrap"]]
    assert len(bootstraps) == 2
    assert slept == [0.4]


def test_bootstrap_gives_up_with_the_supervisor_error(tmp_path, monkeypatch):
    def fake_run(command):
        code = 5 if command[:2] == ["launchctl", "bootstrap"] else 113
        return subprocess.CompletedProcess(
            command, code, "", "Bootstrap failed: 5: Input/output error"
        )

    monkeypatch.setattr("agentroute.bridge_service._run", fake_run)
    monkeypatch.setattr("agentroute.bridge_service.platform.system", lambda: "Darwin")
    monkeypatch.setattr("agentroute.bridge_service.time.sleep", lambda _s: None)

    with pytest.raises(BridgeServiceError, match="after 6 attempts"):
        bridge_service._load_service(tmp_path / "unit.plist")
