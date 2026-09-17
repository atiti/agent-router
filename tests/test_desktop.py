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

    def fake_run(*command, capture=False):
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


def test_desktop_build_rejects_app_server_version_mismatch(tmp_path, monkeypatch):
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
    with pytest.raises(RuntimeError, match="versions differ"):
        desktop.build_desktop_app(source, tmp_path / "ChatGPT-Routed.app")
