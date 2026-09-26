from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

from agentroute import desktop


def _fake_app(path: Path) -> None:
    resources = path / "Contents/Resources"
    resources.mkdir(parents=True)
    (resources / "codex").write_text("stock", encoding="utf-8")
    with (path / "Contents/Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleIdentifier": "com.openai.codex"}, handle)


def test_desktop_build_is_local_reversible_and_preserves_bundle_id(tmp_path, monkeypatch):
    home = tmp_path / "home"
    source = tmp_path / "ChatGPT.app"
    destination = tmp_path / "ChatGPT-Routed.app"
    _fake_app(source)
    (home / "bin").mkdir(parents=True)
    for name in ("codex-bin", "codex-code-mode-host"):
        path = home / "bin" / name
        path.write_text(name, encoding="utf-8")
        path.chmod(0o755)
    (home / "build-id").write_text("test-build\n", encoding="utf-8")
    monkeypatch.setenv("AGENTROUTE_HOME", str(home))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    smoke_calls = []
    run_calls = []
    monkeypatch.setattr(desktop, "smoke_code_mode_host", smoke_calls.append)

    def fake_run(*command, capture=False):
        run_calls.append(command)
        if command[0] == "ditto":
            shutil.copytree(command[1], command[2])
        return subprocess.CompletedProcess(command, 0, stdout="codex-cli test\n", stderr="")

    monkeypatch.setattr(desktop, "_run", fake_run)
    installed, backup = desktop.build_desktop_app(source, destination)
    assert installed == destination
    assert backup is None
    assert (destination / "Contents/Resources/codex-bin").read_text() == "codex-bin"
    with (destination / "Contents/Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    assert info["CFBundleIdentifier"] == "com.openai.codex"
    assert info["CFBundleDisplayName"] == "ChatGPT-Routed"
    assert info["AgentRouteDesktopBuild"] == "test-build"
    assert info["AgentRouteVersion"] == "0.5.51"
    assert "Codex-compatible" in info["CFBundleGetInfoString"]
    assert len(smoke_calls) == 1
    assert smoke_calls[0].name == "codex-code-mode-host"
    helper_signing = [
        command
        for command in run_calls
        if command[0] == "codesign" and Path(command[-1]).name == "codex-code-mode-host"
    ]
    assert len(helper_signing) == 1
    assert "--entitlements" in helper_signing[0]
    assert Path(helper_signing[0][-2]).name == "codex-code-mode-host.entitlements.plist"

    _, backup = desktop.build_desktop_app(source, destination, replace=True)
    assert backup is not None and backup.exists()
    marker = destination / "Contents/Resources/new-build-marker"
    marker.write_text("new", encoding="utf-8")
    restored, replaced = desktop.rollback_desktop_app(destination)
    assert restored == destination
    assert replaced.exists()
    assert not marker.exists()


def test_desktop_build_rejects_non_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop.platform, "system", lambda: "Linux")
    with pytest.raises(RuntimeError, match="only on macOS"):
        desktop.build_desktop_app(tmp_path / "source", tmp_path / "destination")


def test_desktop_build_rejects_concurrent_rebuild(tmp_path, monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENTROUTE_HOME", str(home))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    with (home / "desktop-rebuild.lock").open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="another AgentRoute Desktop build"):
            desktop.build_desktop_app(tmp_path / "source", tmp_path / "destination")


def test_desktop_build_restores_backup_if_destination_appears(tmp_path, monkeypatch):
    home = tmp_path / "home"
    source = tmp_path / "ChatGPT.app"
    destination = tmp_path / "ChatGPT-Routed.app"
    _fake_app(source)
    _fake_app(destination)
    (destination / "original-marker").write_text("original", encoding="utf-8")
    (home / "bin").mkdir(parents=True)
    for name in ("codex-bin", "codex-code-mode-host"):
        path = home / "bin" / name
        path.write_text(name, encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("AGENTROUTE_HOME", str(home))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(desktop, "smoke_code_mode_host", lambda _path: None)

    def fake_run(*command, capture=False):
        if command[0] == "ditto":
            shutil.copytree(command[1], command[2])
        elif Path(command[0]).name == "codex" and command[1:] == ("app-server", "--help"):
            _fake_app(destination)
            (destination / "conflict-marker").write_text("conflict", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="same\n", stderr="")

    monkeypatch.setattr(desktop, "_run", fake_run)
    with pytest.raises(FileExistsError, match="appeared during rebuild"):
        desktop.build_desktop_app(source, destination, replace=True)

    assert (destination / "original-marker").read_text(encoding="utf-8") == "original"
    assert not (destination / "ChatGPT-Routed.app").exists()
    conflicts = list((home / "backups" / "desktop" / "conflicts").glob("*.app"))
    assert len(conflicts) == 1
    assert (conflicts[0] / "conflict-marker").read_text(encoding="utf-8") == "conflict"


def test_desktop_build_rejects_different_codex_release_line(tmp_path, monkeypatch):
    home = tmp_path / "home"
    source = tmp_path / "ChatGPT.app"
    _fake_app(source)
    (home / "bin").mkdir(parents=True)
    for name in ("codex-bin", "codex-code-mode-host"):
        path = home / "bin" / name
        path.write_text(name, encoding="utf-8")
    monkeypatch.setenv("AGENTROUTE_HOME", str(home))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")

    def fake_run(*command, capture=False):
        version = "stock" if str(command[0]).startswith(str(source)) else "routed"
        return subprocess.CompletedProcess(command, 0, stdout=f"{version}\n", stderr="")

    monkeypatch.setattr(desktop, "_run", fake_run)
    with pytest.raises(RuntimeError, match="release versions differ"):
        desktop.build_desktop_app(source, tmp_path / "ChatGPT-Routed.app")


def test_desktop_build_accepts_prerelease_variants_on_same_codex_release(tmp_path, monkeypatch):
    home = tmp_path / "home"
    source = tmp_path / "ChatGPT.app"
    destination = tmp_path / "ChatGPT-Routed.app"
    _fake_app(source)
    (home / "bin").mkdir(parents=True)
    for name in ("codex-bin", "codex-code-mode-host"):
        path = home / "bin" / name
        path.write_text(name, encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("AGENTROUTE_HOME", str(home))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(desktop, "smoke_code_mode_host", lambda _path: None)

    def fake_run(*command, capture=False):
        if command[0] == "ditto":
            shutil.copytree(command[1], command[2])
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        version = (
            "codex-cli 0.155.0-alpha.9.2\n"
            if str(command[0]).startswith(str(source))
            else "codex-cli 0.155.0-alpha.2.6\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout=version, stderr="")

    monkeypatch.setattr(desktop, "_run", fake_run)
    installed, _ = desktop.build_desktop_app(source, destination)

    assert installed == destination
    status = desktop.desktop_status(source, destination)
    assert status["source_routed_versions_match"] is True
    assert status["source_routed_versions_exact_match"] is False
    assert status["source_routed_release_line"] == "0.155.0"
    assert status["routed_release_line"] == "0.155.0"


def test_codex_release_parser_requires_all_three_version_components():
    assert desktop._codex_release_line("codex-cli 0.155.0-alpha.9.2") == "0.155.0"
    assert desktop._codex_release_line("codex-cli 0.155-alpha.9.2") is None
    assert desktop._codex_versions_compatible(
        "codex-cli 0.155.0-alpha.9.2", "codex-cli 0.155.0-alpha.2.6"
    )
    assert not desktop._codex_versions_compatible(
        "codex-cli 0.155.0-alpha.9.2", "codex-cli 0.156.0-alpha.2.6"
    )
