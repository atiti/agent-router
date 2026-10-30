from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from agentroute import release


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "agentroute-darwin-arm64.tar.gz"),
        ("Linux", "x86_64", "agentroute-linux-x64.tar.gz"),
    ],
)
def test_release_asset_name(system, machine, expected, monkeypatch):
    monkeypatch.setattr(release.platform, "system", lambda: system)
    monkeypatch.setattr(release.platform, "machine", lambda: machine)
    assert release.release_asset_name() == expected


def test_release_archive_rejects_parent_traversal(tmp_path: Path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        item = tarfile.TarInfo("../escape")
        item.size = 1
        bundle.addfile(item, io.BytesIO(b"x"))
    with pytest.raises(RuntimeError, match="unsafe path"):
        release._safe_extract(archive, tmp_path / "payload")


@pytest.mark.parametrize("existing_skill", ["absent", "customized", "symlink"])
def test_built_release_installs_progress_skill_without_replacing_existing(
    tmp_path: Path, existing_skill: str
):
    """Exercise real archive assembly and installation without compiling or installing packages."""
    root = Path(__file__).parents[1]
    project = tmp_path / "project"
    for relative in (
        "scripts/build_release_artifact.sh",
        "scripts/install-progress-skill.sh",
        "packaging/install-release.sh",
        "packaging/codex-launcher",
        "skills/goal-checkpoint/SKILL.md",
        "pyproject.toml",
    ):
        destination = project / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, destination)
    # Runtime and wheel validation are independent checks; this fixture exercises packaging.
    smoke = project / "src/agentroute/code_mode_smoke.py"
    smoke.parent.mkdir(parents=True)
    smoke.write_text("pass\n")
    tools = tmp_path / "tools"
    tools.mkdir()

    def executable(path: Path, body: str):
        path.write_text("#!/bin/sh\nset -eu\n" + body)
        path.chmod(0o755)

    executable(tools / "uname", 'case "$1" in -s) echo Linux;; -m) echo x86_64;; esac\n')
    executable(
        tools / "uv",
        'if [ "$1" = build ]; then\n'
        '  while [ "$1" != --out-dir ]; do shift; done\n'
        '  touch "$2/agentroute-0.5.81-py3-none-any.whl"\n'
        'fi\n',
    )
    prebuilt = tmp_path / "prebuilt"
    (prebuilt / "bin").mkdir(parents=True)
    for name in ("codex-bin", "codex-code-mode-host"):
        executable(prebuilt / "bin" / name, "exit 0\n")
    (prebuilt / "build-id").write_text("fixture-build\n")
    installed = tmp_path / "installed"
    (installed / "venv/bin").mkdir(parents=True)
    (installed / "venv/bin/python").symlink_to(sys.executable)
    executable(installed / "venv/bin/agentroute", "exit 0\n")
    user_home = tmp_path / "user"
    user_home.mkdir()
    codex_home = tmp_path / "custom-codex-home"
    skill = codex_home / "skills/goal-checkpoint"
    if existing_skill == "customized":
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("user customization\n")
    elif existing_skill == "symlink":
        skill.parent.mkdir(parents=True)
        skill.symlink_to(tmp_path / "missing-user-skill")
    env = {
        **os.environ,
        "PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
        "HOME": str(user_home),
        "SHELL": "/bin/sh",
        "TMPDIR": str(tmp_path),
        "CODEX_HOME": str(codex_home),
        "AGENTROUTE_HOME": str(installed),
        "AGENTROUTE_RELEASE_PREBUILT_HOME": str(prebuilt),
    }
    output = tmp_path / "dist"
    subprocess.run(
        ["sh", str(project / "scripts/build_release_artifact.sh"), str(output)],
        env=env, check=True, capture_output=True, text=True,
    )
    payload = tmp_path / "payload"
    release._safe_extract(output / "agentroute-linux-x64.tar.gz", payload)
    subprocess.run(
        ["sh", str(payload / "install.sh")],
        env=env, check=True, capture_output=True, text=True,
    )
    if existing_skill == "symlink":
        assert skill.is_symlink()
        assert skill.readlink() == tmp_path / "missing-user-skill"
    else:
        expected = (
            "user customization\n" if existing_skill == "customized"
            else (root / "skills/goal-checkpoint/SKILL.md").read_text()
        )
        assert (skill / "SKILL.md").read_text() == expected
    assert not (user_home / ".codex/skills/goal-checkpoint").exists()


def test_release_installer_is_transactional_and_validates_runtime():
    installer = (Path(__file__).parents[1] / "packaging" / "install-release.sh").read_text()

    assert "agentroute-release-rollback" in installer
    assert "atomic_install_file" in installer
    assert 'mv -f "$STAGED" "$DESTINATION"' in installer
    assert 'cp "$PAYLOAD_ROOT/codex-bin" "$AGENTROUTE_BIN_DIR/codex-bin"' not in installer
    assert "restored the previous runtime files" in installer
    assert 'codex-bin" --version' in installer
    assert "code-mode-smoke.py" in installer
    assert "codex-code-mode-host" in installer
    assert "valid Developer ID signature" in installer
    assert installer.index("code-mode-smoke.py") < installer.index("INSTALL_COMMITTED=1")


def test_macos_release_build_fails_closed_without_developer_id():
    builder = (Path(__file__).parents[1] / "scripts" / "build_release_artifact.sh").read_text()

    assert "Refusing to build a public macOS release without a Developer ID identity" in builder
    assert "Authority=Developer ID Application:" in builder
    assert "xcrun notarytool submit" in builder
    assert "--wait" in builder
    assert "spctl --assess" not in builder
    assert "codex-code-mode-host.entitlements.plist" in builder
    assert "code-mode-smoke.py" in builder
    assert 'cp "$PROJECT_ROOT/src/agentroute/code_mode_smoke.py"' in builder


def test_release_workflow_uses_overwatchr_signing_secret_names():
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "release.yml").read_text()

    for name in (
        "MACOS_CERTIFICATE_P12_BASE64",
        "MACOS_CERTIFICATE_PASSWORD",
        "MACOS_CODESIGN_IDENTITY",
        "APPLE_ID",
        "APPLE_TEAM_ID",
        "APPLE_APP_SPECIFIC_PASSWORD",
    ):
        assert f"secrets.{name}" in workflow


def test_update_refuses_to_downgrade_newer_source_install():
    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        release._ensure_not_downgrade("0.5.19", "0.5.20")


def test_update_allows_same_or_newer_release():
    release._ensure_not_downgrade("0.5.20", "0.5.20")
    release._ensure_not_downgrade("0.5.21", "0.5.20")
