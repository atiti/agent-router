import os
from pathlib import Path

from agentroute.codex_patch import install_binary, patch_path, patch_paths


def test_native_patch_is_packaged():
    content = "\n".join(path.read_text() for path in patch_paths())

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
    assert "guardian review session could not disable incompatible" in content
    assert "normalize_prompt_for_provider" in content
    assert "openai_prompt_drops_third_party_plaintext_reasoning" in content
    assert "compatible_third_party_provider_drops_encrypted_provider_state" in content
    assert "final_request_boundary_drops_third_party_encrypted_state" in content
    assert "normalize_response_items_for_provider(" in content
    assert "self.strip_unattributed_provider_state" in content
    assert "guardian_review_session_config_uses_routed_turn_provider" in content
    assert "compatibility: Option<ToolCompatibility>" in content
    assert "if let Some((_, tool_compatibility, _)) = requested_provider.as_ref()" in content
    assert "routed_parent_config.model_provider = provider.info().clone()" in content
    assert "approval_review_model" in content
    assert "config.model_provider.tool_compatibility" in content
    assert "stripPromptPrefixBytes" in content
    assert "stripProviderState" in content
    assert "new_session_for_mixed_provider_history" in content
    assert "Route the turn before pre-sampling compaction" in content
    assert "inspect_input_hooks(&sess, &turn_context, &input).await" in content
    assert "model_info_for_provider_compatibility" in content
    assert "client_session: &mut ModelClientSession" in content
    assert "run_inline_auto_compact_task" in content
    assert "request_step_context" in content
    assert "skip_previous_model_compact" in content
    assert "model_provider_id" in content
    assert "foreign_provider_state_ids" in content
    assert "history_has_foreign_provider_state" in content
    assert "SessionSource::VSCode" in content
    assert "MessagePhase::Commentary" in content
    assert "emit_turn_item_completed" in content
    assert "not added to the model's input history" in content
    assert "routing_prompt" in content
    assert "Some(args.task_name.clone())" in content
    assert '!properties.contains_key("routing_prompt")' in content
    assert "response_input_serialization_excludes_ephemeral_routing_prompt" in content
    assert "encrypted_subagent_input_uses_ephemeral_routing_prompt" in content
    assert "encrypted_communication_keeps_ephemeral_routing_prompt_in_memory" in content
    # App-server quota polling is propagated into each live thread so the local hook
    # makes a safe decision without a network call on every user turn.
    assert "ordinary_usage_allowed" in content
    assert "capacity_snapshot" in content
    assert "record_ordinary_usage_allowed" in content
    assert "account/rateLimits/read" in content
    continuation_test = (
        "mixed_provider_history_preserves_destination_reasoning_across_tool_continuations"
    )
    assert continuation_test in content


def test_primary_patch_path_is_backwards_compatible():
    assert patch_path() == patch_paths()[0]


def test_install_binary_atomically_replaces_destination(tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "bin" / "codex-bin"
    source.write_bytes(b"new binary")
    destination.parent.mkdir()
    destination.write_bytes(b"old binary")
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def record_replace(staged: Path, installed: Path) -> None:
        assert staged.parent == destination.parent
        assert staged.read_bytes() == b"new binary"
        assert installed.read_bytes() == b"old binary"
        replacements.append((staged, installed))
        real_replace(staged, installed)

    monkeypatch.setattr("agentroute.codex_patch.os.replace", record_replace)

    install_binary(source, destination)

    assert replacements
    assert destination.read_bytes() == b"new binary"
    assert destination.stat().st_mode & 0o111


def test_installer_enables_code_mode_and_signs_macos_binary():
    installer = (Path(__file__).parents[1] / "scripts" / "install.sh").read_text()
    launcher = (Path(__file__).parents[1] / "src/agentroute/launcher.py").read_text()

    assert '"code_mode"' in launcher
    assert '"code_mode_host"' in launcher
    assert "codex-code-mode-host" in installer
    assert "AGENTROUTE_CODE_MODE_HOST_VERSION" in installer
    assert 'npm pack \\' in installer
    assert "-p codex-code-mode-host --bin codex-code-mode-host" not in installer
    packaged_launcher = (
        Path(__file__).parents[1] / "packaging" / "codex-launcher"
    ).read_text()
    assert "classifier-refresh" in packaged_launcher
    assert 'cp "$AGENTROUTE_PROJECT_ROOT/packaging/codex-launcher"' in installer
    assert "codesign --force --deep --sign -" in installer
    assert "codesign --verify --deep --strict" in installer
    assert '"$AGENTROUTE_STAGED_CODEX" --version' in installer
    assert "codex-code-mode-host.entitlements.plist" in installer
    assert "code_mode_smoke.py" in installer
    assert 'AGENTROUTE_CODEX_TARGET=${AGENTROUTE_CODEX_TARGET:-' in installer
    assert "codex-provider-provenance.patch" not in installer
    assert "provider-routing-v30" in installer
    assert "chatgpt_profile_home" in installer
    assert "ordinary_usage_allowed" in installer
    assert "codex-desktop-route-notice.patch" in installer
    assert "codex-package-version.patch" in installer
    assert "0.155.0-alpha.2.7" in "\n".join(
        path.read_text() for path in patch_paths()
    )
    assert "apply --recount" in installer
    assert "MessagePhase::Commentary" in installer
    assert 'version = "0.155.0-alpha.2.7"' in installer
    assert "Some(args.task_name.clone())" in installer
    assert '\"routing_prompt\".to_string()' in installer
    same_host_guard = (
        'AGENTROUTE_SOURCE_CODE_MODE_HOST" != "$AGENTROUTE_BIN_DIR/codex-code-mode-host'
    )
    assert same_host_guard in installer
