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
    assert "modelProvider" in content
    assert "apply_routed_turn_settings" in content
    assert "new_session_for_provider" in content
    assert "TurnInput::InterAgentCommunication" in content
    assert "ToolCompatibility::FunctionsAndApplyPatch" in content
    assert "provider_tool_compatibility_preserves_admitted_safety_authority" in content
    assert "normalize_prompt_for_provider" in content
    assert "openai_prompt_drops_third_party_plaintext_reasoning" in content
    assert "compatible_third_party_provider_drops_encrypted_provider_state" in content
    assert "final_request_boundary_drops_third_party_encrypted_state" in content
    assert "normalize_response_items_for_provider(input, self.state.provider.info())" in content


def test_installer_enables_code_mode_and_signs_macos_binary():
    installer = (Path(__file__).parents[1] / "scripts" / "install.sh").read_text()

    assert "--enable code_mode" in installer
    assert "--enable code_mode_host" in installer
    assert "codex-code-mode-host" in installer
    assert "AGENTROUTE_CODE_MODE_HOST_VERSION" in installer
    assert 'npm pack \\' in installer
    assert "-p codex-code-mode-host --bin codex-code-mode-host" not in installer
    assert "classifier-refresh" in installer
    assert "codesign --force --sign -" in installer
    assert 'AGENTROUTE_CODEX_TARGET=${AGENTROUTE_CODEX_TARGET:-' in installer
