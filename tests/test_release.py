from __future__ import annotations

import io
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


def test_release_installer_is_transactional_and_validates_runtime():
    installer = (Path(__file__).parents[1] / "packaging" / "install-release.sh").read_text()

    assert "agentroute-release-rollback" in installer
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
