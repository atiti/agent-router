import os
import subprocess
from pathlib import Path

from agentroute.codex_patch import install_binary, patch_path, patch_paths


def test_native_patch_is_packaged():
    content = "\n".join(path.read_text() for path in patch_paths())

    assert "reasoningEffort" in content
    assert '"ultra" => ReasoningEffortConfig::Ultra' in content
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
    assert "fn normalize_prompt_for_provider" not in content
    assert "compaction_survives_same_provider_but_not_provider_or_account_switch" in content
    assert "let mut model_info = destination.clone();" in content
    assert "openai_prompt_drops_third_party_plaintext_reasoning" in content
    assert "compatible_third_party_provider_drops_encrypted_provider_state" in content
    assert "final_request_boundary_drops_third_party_encrypted_state" in content
    assert "normalize_response_items_for_provider(" in content
    assert "self.strip_unattributed_provider_state" in content
    assert "guardian_review_session_config_uses_routed_turn_provider" in content
    assert "compatibility: Option<ToolCompatibility>" in content
    assert "Some(tool_compatibility)," in content
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
    assert "new_turn_reapplies_provider_tool_compatibility_after_model_resolution" in content
    assert (
        "new_turn_without_provider_tool_compatibility_preserves_fallback_metadata"
        in content
    )
    assert "direct_provider_compatibility_hides_code_mode_exec" in content
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
    assert "routing_inherited_model_provider" in content
    assert "routing_requested_backend" in content
    assert "routing_model_explicit" in content
    assert "make_inter_agent_input_portable_for_provider" not in content
    assert "cross_provider_inter_agent_input_converts_payload_to_plaintext" not in content
    assert 'DEFAULT_MULTI_AGENT_V2_TOOL_NAMESPACE: &str = "agentroute_collaboration"' in content
    assert "spawn_agent_tool_v2_requires_task_name_and_lists_visible_models" in content
    assert "namespace == DEFAULT_MULTI_AGENT_V2_TOOL_NAMESPACE" in content
    assert '"spawn_agent" | "send_message" | "followup_task"' in content
    assert "message.encrypted = None;" in content
    assert "plaintext_v2_custom_namespace_calls_are_redacted_without_encryption_metadata" in content
    assert "encrypted_v2_collaboration_calls_remain_encrypted" in content
    assert "reserved_collaboration_calls_without_metadata_remain_provider_managed" in content
    assert "multi_agent_v2_reserved_namespace_message_schemas_are_encrypted" in content
    assert ".is_none_or(Vec::is_empty)" in content
    assert "plaintext_communication_renders_exact_task_for_cross_provider_delivery" in content
    assert "Payload:\\nReply with exactly: deepseek child ok" in content
    assert "provider_capabilities" in content
    assert ".namespace_tools" in content
    assert 'format!("functions.{namespace}.spawn_agent")' in content
    assert '"functions.collaboration.spawn_agent".to_string()' in content
    assert "fn tool_dispatch_payload(payload: &ToolPayload, source: &ToolCallSource)" in content
    assert "matches!(source, ToolCallSource::DirectPlaintextMessage)" in content
    assert 'arguments: "{}".to_string()' in content
    assert "ToolDispatchPayload::Function" in content
    assert "AgentMessageMetadata" in content
    assert "communication_id: Some(agent_message_id(tool_call_id))" in content
    assert "fn agent_message_id(tool_call_id: &str) -> String" in content
    assert 'format!("amsg_{tool_call_id}")' in content
    assert "sanitize_inference_request" in content
    assert "inference_trace_redacts_only_plaintext_collaboration_messages" in content
    assert '"backend".to_string()' not in content
    assert "+    backend: Option<String>," not in content
    assert "properties.keys().map(String::as_str).collect::<Vec<_>>()" in content
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


def test_release_metadata_uses_v0542_runtime_v37():
    root = Path(__file__).parents[1]
    package = (root / "pyproject.toml").read_text(encoding="utf-8")
    lock = (root / "uv.lock").read_text(encoding="utf-8")
    public_api = (root / "src/agentroute/__init__.py").read_text(encoding="utf-8")
    installer = (root / "scripts/install.sh").read_text(encoding="utf-8")
    doctor = (root / "src/agentroute/doctor.py").read_text(encoding="utf-8")
    ci = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert 'version = "0.5.42"' in package
    assert 'version = "0.5.42"' in lock
    assert '__version__ = "0.5.42"' in public_api
    assert "provider-routing-v37" in installer
    assert 'EXPECTED_RUNTIME_REVISION = "provider-routing-v37"' in doctor
    assert "b412ff32c417f855c2b2d1581b77058eed87c84b" in installer
    assert "0.156.1" in installer
    assert "b412ff32c417f855c2b2d1581b77058eed87c84b" in ci


def test_installer_preserves_dirty_source_before_upstream_upgrade(tmp_path):
    origin = tmp_path / "origin"
    source = tmp_path / "codex"
    origin.mkdir()

    def git(cwd, *args):
        return subprocess.check_output(
            ["git", "-C", str(cwd), *args], text=True, stderr=subprocess.STDOUT
        ).strip()

    git(origin, "init")
    git(origin, "config", "user.name", "Test")
    git(origin, "config", "user.email", "test@example.invalid")
    (origin / "source.rs").write_text("old upstream\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "old")
    old = git(origin, "rev-parse", "HEAD")
    (origin / "source.rs").write_text("new upstream\n")
    git(origin, "commit", "-am", "new")
    new = git(origin, "rev-parse", "HEAD")
    git(tmp_path, "clone", str(origin), str(source))
    git(source, "checkout", "--detach", old)
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "source.rs").write_text("old AgentRoute patches\n")
    git(source, "add", "source.rs")
    (source / "source.rs").write_text("additional local edits\n")
    (source / "local-note.txt").write_text("preserve untracked work\n")
    installer = (Path(__file__).parents[1] / "scripts/install.sh").read_text()
    start = installer.index('git -C "$AGENTROUTE_CODEX_SOURCE" fetch')
    end = installer.index("AGENTROUTE_PATCHES_APPLIED=0", start)
    subprocess.run(
        ["sh", "-eu", "-c", installer[start:end]],
        env={**os.environ, "AGENTROUTE_CODEX_SOURCE": str(source), "AGENTROUTE_CODEX_COMMIT": new},
        check=True, capture_output=True, text=True,
    )
    assert git(source, "rev-parse", "HEAD") == new
    assert git(source, "status", "--porcelain") == ""
    assert git(source, "show", "stash@{0}:source.rs") == "additional local edits"
    assert git(source, "show", "stash@{0}^2:source.rs") == "old AgentRoute patches"
    assert git(source, "show", "stash@{0}^3:local-note.txt") == "preserve untracked work"


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
    assert "provider-routing-v37" in installer
    assert "chatgpt_profile_home" in installer
    assert "ordinary_usage_allowed" in installer
    assert "codex-package-version.patch" in installer
    assert "codex-history-recovery.patch" in installer
    assert "routed_turn_model_provider" in "\n".join(
        path.read_text() for path in patch_paths()
    )
    assert "model_with_provider_display_name" in "\n".join(
        path.read_text() for path in patch_paths()
    )
    assert "reviewerFallbackProfiles" in "\n".join(
        path.read_text() for path in patch_paths()
    )
    assert "ReviewerFallbackReady" in "\n".join(
        path.read_text() for path in patch_paths()
    )
    assert "APPROVAL REVIEWER FALLBACK" in "\n".join(
        path.read_text() for path in patch_paths()
    )
    assert "0.155.0-alpha.2.6" in "\n".join(
        path.read_text() for path in patch_paths()
    )
    assert "Recover interrupted custom calls in debug builds too." in "\n".join(
        path.read_text() for path in patch_paths()
    )
    assert "apply --recount" in installer
    assert "MessagePhase::Commentary" in installer
    assert 'version = "0.155.0-alpha.2.6"' in installer
    assert "Some(args.task_name.clone())" in installer
    assert '\"routing_prompt\".to_string()' in installer
    assert "routing_inherited_model_provider" in installer
    assert "routing_requested_backend" in installer
    assert "routing_model_explicit" in installer
    assert 'properties.keys().map(String::as_str).collect::<Vec<_>>()' in installer
    assert "! grep -F '\"backend\".to_string()'" in installer
    assert "! grep -F '    backend: Option<String>,'" in installer
    same_host_guard = (
        'AGENTROUTE_SOURCE_CODE_MODE_HOST" != "$AGENTROUTE_BIN_DIR/codex-code-mode-host'
    )
    assert same_host_guard in installer
