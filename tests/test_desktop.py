from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

from agentroute import __version__, desktop


def test_open_official_desktop_preserves_app_identity_and_passes_runtime(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agentroute.cli import app

    source = tmp_path / "Official App.app"
    source.mkdir()
    (source / "Contents/MacOS").mkdir(parents=True)
    (source / "Contents/MacOS/ChatGPT").write_text("official")
    runtime = tmp_path / "runtime with spaces/codex"
    runtime.parent.mkdir()
    runtime.write_text("launcher")
    runtime.chmod(0o755)
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    calls = []
    monkeypatch.setattr(desktop, "_run", lambda *command, **kwargs: calls.append(command))
    result = CliRunner().invoke(
        app, ["desktop", "open", "--source", str(source), "--runtime", str(runtime)]
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        ("open", "-a", source, "--env", f"CODEX_CLI_PATH={runtime.resolve()}")
    ]
    assert (source / "Contents/MacOS/ChatGPT").read_text() == "official"


def test_open_official_desktop_rejects_missing_runtime_before_launch(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agentroute.cli import app

    source = tmp_path / "Official.app"
    source.mkdir()
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    calls = []
    monkeypatch.setattr(desktop, "_run", lambda *command, **kwargs: calls.append(command))
    result = CliRunner().invoke(
        app, ["desktop", "open", "--source", str(source), "--runtime", str(tmp_path / "missing")]
    )
    assert result.exit_code == 1
    assert calls == []
    assert "runtime" in result.output.lower()


def test_open_official_desktop_defaults_to_installed_launcher(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agentroute.cli import app

    source = tmp_path / "Official.app"
    source.mkdir()
    runtime = tmp_path / "bin/codex"
    runtime.parent.mkdir()
    runtime.write_text("launcher")
    runtime.chmod(0o755)
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    calls = []
    monkeypatch.setattr(desktop, "_run", lambda *command, **kwargs: calls.append(command))
    result = CliRunner().invoke(app, ["desktop", "open", "--source", str(source)])
    assert result.exit_code == 0, result.output
    assert calls == [("open", "-a", source, "--env", f"CODEX_CLI_PATH={runtime.resolve()}")]


def _fake_app(path: Path) -> None:
    resources = path / "Contents/Resources"
    resources.mkdir(parents=True)
    (resources / "codex").write_text("stock", encoding="utf-8")
    with (path / "Contents/Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleIdentifier": "com.openai.codex"}, handle)


@pytest.mark.parametrize("signed_runtime", [False, True])
def test_desktop_build_is_local_reversible_and_preserves_bundle_id(
    tmp_path, monkeypatch, signed_runtime
):
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
        if command[:2] == ("codesign", "-dv"):
            return subprocess.CompletedProcess(
                command, 0, stdout="", stderr=(
                    "Authority=Developer ID Application: Example\nTeamIdentifier=EXAMPLE123\n"
                    if signed_runtime else "Signature=adhoc\nTeamIdentifier=not set\n"
                ),
            )
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
    assert info["AgentRouteVersion"] == __version__
    assert "Codex-compatible" in info["CFBundleGetInfoString"]
    assert len(smoke_calls) == 1
    assert smoke_calls[0].name == "codex-code-mode-host"
    helper_signing = [
        command
        for command in run_calls
        if command[0] == "codesign" and "--sign" in command
        and Path(command[-1]).name == "codex-code-mode-host"
    ]
    if signed_runtime:
        assert helper_signing == []
        assert not any(
            command[0] == "codesign" and "--sign" in command
            and Path(command[-1]).name == "codex-bin" for command in run_calls
        )
        app_signing = [c for c in run_calls if c[0] == "codesign" and "--sign" in c
                       and str(c[-1]).endswith(".app")]
        # Nested frameworks/helpers must still be re-signed with the outer identity.
        assert app_signing and all("--deep" in c for c in app_signing)
    else:
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


def test_desktop_build_warns_but_accepts_different_codex_release_line(tmp_path, monkeypatch):
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
        if command[1:] == ("--version",):
            version = (
                "codex-cli 0.158.0-alpha.2.1\n"
                if str(command[0]).startswith(str(source))
                else "codex-cli 0.159.2\n"
            )
            return subprocess.CompletedProcess(command, 0, stdout=version, stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(desktop, "_run", fake_run)
    with pytest.warns(
        UserWarning, match="frontend/app-server protocol compatibility is not guaranteed"
    ):
        installed, _ = desktop.build_desktop_app(source, destination)

    assert installed == destination
    status = desktop.desktop_status(source, destination)
    assert status["source_routed_versions_match"] is False
    assert status["source_routed_release_line"] == "0.158.0"
    assert status["routed_release_line"] == "0.159.2"


def test_desktop_build_can_strictly_reject_a_version_mismatch(tmp_path, monkeypatch):
    home = tmp_path / "home"
    source = tmp_path / "ChatGPT.app"
    _fake_app(source)
    (home / "bin").mkdir(parents=True)
    for name in ("codex-bin", "codex-code-mode-host"):
        (home / "bin" / name).write_text(name, encoding="utf-8")
    monkeypatch.setenv("AGENTROUTE_HOME", str(home))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")

    def fake_run(*command, capture=False):
        version = (
            "codex-cli 0.158.0-alpha.2.1\n"
            if str(command[0]).startswith(str(source))
            else "codex-cli 0.159.2\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout=version, stderr="")

    monkeypatch.setattr(desktop, "_run", fake_run)
    with pytest.raises(RuntimeError, match="strict version matching is enabled"):
        desktop.build_desktop_app(
            source,
            tmp_path / "ChatGPT-Routed.app",
            strict_version_match=True,
        )


@pytest.mark.parametrize("release,expected_transport", [("0.155.0", "framed"), ("0.172.0", "grpc")])
def test_desktop_build_accepts_prerelease_variants_on_same_codex_release(
    tmp_path, monkeypatch, release, expected_transport
):
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
    transports = []
    monkeypatch.setattr(desktop, "smoke_code_mode_host", lambda _path: transports.append("framed"))
    monkeypatch.setattr(desktop, "smoke_grpc_host", lambda _path: transports.append("grpc"))

    def fake_run(*command, capture=False):
        if command[0] == "ditto":
            shutil.copytree(command[1], command[2])
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        version = (
            f"codex-cli {release}-alpha.9.2\n"
            if str(command[0]).startswith(str(source))
            else f"codex-cli {release}-alpha.2.6\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout=version, stderr="")

    monkeypatch.setattr(desktop, "_run", fake_run)
    installed, _ = desktop.build_desktop_app(source, destination)

    assert installed == destination
    assert transports == [expected_transport]
    status = desktop.desktop_status(source, destination)
    assert status["source_routed_versions_match"] is True
    assert status["source_routed_versions_exact_match"] is False
    assert status["source_routed_release_line"] == release
    assert status["routed_release_line"] == release


def test_codex_release_parser_requires_all_three_version_components():
    assert desktop._codex_release_line("codex-cli 0.155.0-alpha.9.2") == "0.155.0"
    assert desktop._codex_release_line("codex-cli 0.155-alpha.9.2") is None
    assert desktop._codex_versions_compatible(
        "codex-cli 0.155.0-alpha.9.2", "codex-cli 0.155.0-alpha.2.6"
    )
    assert not desktop._codex_versions_compatible(
        "codex-cli 0.155.0-alpha.9.2", "codex-cli 0.156.0-alpha.2.6"
    )


@pytest.mark.parametrize(
    "relative", ["codex", "codex-cli/bin/codex", "codex-cli/CodexCLI.app/Contents/MacOS/codex"]
)
def test_desktop_detects_supported_cli_layouts(tmp_path, relative):
    executable = tmp_path / "Contents/Resources" / relative
    executable.parent.mkdir(parents=True)
    executable.write_text("binary")
    assert desktop.desktop_codex_path(tmp_path) == executable


def test_packaged_desktop_replaces_frontend_entrypoints(tmp_path, monkeypatch):
    home = tmp_path / "home"
    source = tmp_path / "ChatGPT.app"
    destination = tmp_path / "ChatGPT-Routed.app"
    _fake_app(source)
    (source / "Contents/Resources/codex").unlink()
    entrypoints = ("codex-cli/bin/codex", "codex-cli/CodexCLI.app/Contents/MacOS/codex")
    for relative in entrypoints:
        path = source / "Contents/Resources" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stock")
    (home / "bin").mkdir(parents=True)
    for name in ("codex-bin", "codex-code-mode-host"):
        (home / "bin" / name).write_text(name)
    (home / "build-id").write_text("new-build\n")
    monkeypatch.setenv("AGENTROUTE_HOME", str(home))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(desktop, "smoke_code_mode_host", lambda _path: None)
    calls = []

    def fake_run(*command, capture=False):
        calls.append(command)
        if command[0] == "ditto":
            shutil.copytree(command[1], command[2])
        return subprocess.CompletedProcess(
            command, 0, stdout="codex-cli 0.158.0-alpha.2.1\n", stderr=""
        )

    monkeypatch.setattr(desktop, "_run", fake_run)
    desktop.build_desktop_app(source, destination)
    for relative in entrypoints:
        assert (destination / "Contents/Resources" / relative).read_bytes() == (
            desktop._assets_dir() / "codex-launcher"
        ).read_bytes()
    assert (source / "Contents/Resources/codex-cli/bin/codex").read_text() == "stock"
    status = desktop.desktop_status(source, destination)
    assert status["destination_build_id"] == "new-build"
    assert status["destination_matches_runtime"] is True
    assert "codex-cli/bin/codex" in status["source_cli_path"]
    assert any(
        str(call[0]).endswith("codex-cli/bin/codex") and call[1:] == ("app-server", "--help")
        for call in calls
    )


def test_desktop_missing_cli_reports_path_before_version_mismatch(tmp_path, monkeypatch):
    home = tmp_path / "home"
    source = tmp_path / "ChatGPT.app"
    source.mkdir()
    (home / "bin").mkdir(parents=True)
    for name in ("codex-bin", "codex-code-mode-host"):
        (home / "bin" / name).write_text(name)
    monkeypatch.setenv("AGENTROUTE_HOME", str(home))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    with pytest.raises(FileNotFoundError, match="official Codex CLI not found"):
        desktop.build_desktop_app(source, tmp_path / "Routed.app")
