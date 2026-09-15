from pathlib import Path

from agentroute.codex_patch import patch_path


def test_native_patch_is_packaged():
    content = patch_path().read_text()

    assert "reasoningEffort" in content
    assert "settings_updated" in content
    assert "UserPromptSubmit" in content
    assert "MODEL ROUTE" in content
    assert "routeMessage" in content
    assert "routed_turn_model" in content


def test_installer_enables_code_mode_and_signs_macos_binary():
    installer = (Path(__file__).parents[1] / "scripts" / "install.sh").read_text()

    assert "--enable code_mode" in installer
    assert "codex-code-mode-host" in installer
    assert "codesign --force --sign -" in installer
