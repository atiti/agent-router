import io
import json

from agentroute.audit import AuditStore
from agentroute.config import default_config
from agentroute.hook import codex_stop, codex_user_prompt_submit


class FakeClassifierResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        content = json.dumps(
            {
                "tier": "SMART",
                "confidence": 0.87,
                "task_type": "implementation",
                "reason": "The task requires substantial implementation judgment.",
            }
        )
        return json.dumps(
            {
                "model": "classifier-fast",
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
                "choices": [{"message": {"content": content}}],
            }
        ).encode()


def invoke(
    config,
    store,
    prompt,
    model="gpt-5.6-terra",
    transcript_path=None,
    subagent=None,
    model_provider="openai",
    flat_agent_id=None,
    flat_agent_type=None,
):
    source = io.StringIO(
        json.dumps(
            {
                "session_id": "same-thread",
                "turn_id": "turn-1",
                "model": model,
                "model_provider": model_provider,
                "prompt": prompt,
                "subagent": subagent,
                "agent_id": flat_agent_id,
                "agent_type": flat_agent_type,
                "transcript_path": str(transcript_path) if transcript_path else None,
            }
        )
    )
    sink = io.StringIO()
    assert codex_user_prompt_submit(source, sink, config=config, store=store) == 0
    return json.loads(sink.getvalue())


def test_observe_mode_does_not_emit_override(tmp_path):
    config = default_config()
    output = invoke(config, AuditStore(tmp_path / "audit.db"), "@smart investigate")
    specific = output["hookSpecificOutput"]

    assert "Would select SMART" in specific["additionalContext"]
    assert "model" not in specific
    assert "reasoningEffort" not in specific


def test_enabled_mode_emits_native_override_and_keeps_session_history(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTROUTE_RUNTIME_BUILD_ID", raising=False)
    config = default_config()
    config.enabled = True
    store = AuditStore(tmp_path / "audit.db")

    first = invoke(config, store, "@smart investigate")
    second = invoke(config, store, "go ahead")

    assert first["hookSpecificOutput"]["model"] == "gpt-5.6-sol"
    assert first["hookSpecificOutput"]["reasoningEffort"] == "high"
    assert first["hookSpecificOutput"]["routeMessage"] == (
        "◆ MODEL ROUTE · SMART → gpt-5.6-sol · high reasoning "
        "· backend gpt/openai · scope root · source MANUAL "
        "· rule confidence 100% · rule score -0.5"
    )
    assert second["hookSpecificOutput"]["model"] == "gpt-5.6-sol"
    assert len(store.history("same-thread")) == 2


def test_explicit_backend_is_sticky_and_directive_is_hidden_from_model(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    store = AuditStore(tmp_path / "audit.db")

    first = invoke(config, store, "@azure check status and eta")
    second = invoke(config, store, "I wanted you to check the status of the CRM sync")

    assert first["hookSpecificOutput"]["modelProvider"] == "agentroute-azure"
    assert first["hookSpecificOutput"]["stripProviderState"] is True
    assert first["hookSpecificOutput"]["stripPromptPrefixBytes"] == len("@azure ")
    assert second["hookSpecificOutput"]["modelProvider"] == "agentroute-azure"
    assert second["hookSpecificOutput"]["model"] == first["hookSpecificOutput"]["model"]
    assert second["hookSpecificOutput"]["stripProviderState"] is True
    assert "stripPromptPrefixBytes" not in second["hookSpecificOutput"]
    assert "SESSION_AFFINITY" in store.latest("same-thread")["reason_codes"]


def test_auto_clears_explicit_backend_affinity(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    store = AuditStore(tmp_path / "audit.db")

    invoke(config, store, "@azure say hi")
    cleared = invoke(config, store, "@auto say hi")
    followup = invoke(config, store, "say hi again")

    assert cleared["hookSpecificOutput"]["modelProvider"] == "openai"
    assert followup["hookSpecificOutput"]["modelProvider"] == "openai"


def test_backend_affinity_is_scoped_away_from_subagents(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    store = AuditStore(tmp_path / "audit.db")

    invoke(config, store, "@azure say hi")
    child = invoke(
        config,
        store,
        "say hi",
        subagent={"agent_id": "/root/worker", "agent_type": "worker"},
    )

    assert child["hookSpecificOutput"]["modelProvider"] == "openai"


def test_route_message_identifies_managed_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AGENTROUTE_RUNTIME_BUILD_ID",
        "upstream-commit-provider-routing-v8",
    )
    config = default_config()
    config.enabled = True

    output = invoke(config, AuditStore(tmp_path / "audit.db"), "@fast say hi")

    assert output["hookSpecificOutput"]["routeMessage"].endswith(" · runtime v8")


def test_subagent_task_is_independently_routed_and_audited(tmp_path):
    config = default_config()
    config.enabled = True
    store = AuditStore(tmp_path / "audit.db")

    output = invoke(
        config,
        store,
        "Rename the internal label and fix the typo",
        subagent={"agent_id": "/root/mechanical", "agent_type": "worker"},
    )
    row = store.latest("same-thread")

    assert output["hookSpecificOutput"]["model"] == "gpt-5.6-luna"
    assert output["hookSpecificOutput"]["modelProvider"] == "openai"
    assert "scope subagent" in output["hookSpecificOutput"]["routeMessage"]
    assert row["route_scope"] == "subagent"
    assert row["agent_id"] == "/root/mechanical"


def test_flat_codex_subagent_fields_are_routed_and_audited(tmp_path):
    config = default_config()
    config.enabled = True
    store = AuditStore(tmp_path / "audit.db")

    output = invoke(
        config,
        store,
        "Design a zero-downtime database migration",
        flat_agent_id="0199-child",
        flat_agent_type="worker",
    )
    row = store.latest("same-thread")

    assert "scope subagent" in output["hookSpecificOutput"]["routeMessage"]
    assert row["route_scope"] == "subagent"
    assert row["agent_id"] == "0199-child"


def test_opaque_subagent_followup_inherits_its_own_route(tmp_path):
    config = default_config()
    config.enabled = True
    store = AuditStore(tmp_path / "audit.db")

    invoke(config, store, "@smart architecture review", flat_agent_id="child-a")
    invoke(config, store, "@fast rename a label", flat_agent_id="child-b")
    child_a_followup = invoke(
        config,
        store,
        "",
        model="gpt-5.6-sol",
        flat_agent_id="child-a",
    )
    child_b_followup = invoke(
        config,
        store,
        "",
        model="gpt-5.6-luna",
        flat_agent_id="child-b",
    )

    assert child_a_followup["hookSpecificOutput"]["model"] == "gpt-5.6-sol"
    assert child_b_followup["hookSpecificOutput"]["model"] == "gpt-5.6-luna"
    child_rows = [row for row in store.history("same-thread") if row["agent_id"]]
    assert child_rows[0]["classification_source"] == "session_affinity"
    assert child_rows[1]["classification_source"] == "session_affinity"
    assert child_rows[0]["agent_id"] == "child-b"
    assert child_rows[1]["agent_id"] == "child-a"


def test_confirmation_uses_previous_assistant_task_definition(tmp_path):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                "Redesign the authentication architecture and perform a "
                                "zero-downtime database migration across the services."
                            ),
                        }
                    ],
                },
            }
        )
        + "\n"
    )
    config = default_config()
    config.enabled = True
    store = AuditStore(tmp_path / "audit.db")

    output = invoke(config, store, "ok do it", transcript_path=transcript)
    row = store.latest("same-thread")

    assert output["hookSpecificOutput"]["model"] == "gpt-5.6-sol"
    assert "MAX→SMART SAFETY FALLBACK" in output["hookSpecificOutput"]["routeMessage"]
    assert row is not None
    assert row["task_context_used"] == 1
    assert "TASK_DEFINITION_INHERITANCE" in row["reason_codes"]


def test_credential_route_notice_is_visible(tmp_path):
    config = default_config()
    config.enabled = True

    output = invoke(
        config,
        AuditStore(tmp_path / "audit.db"),
        "service api key: abcdefghijklmnop1234",
    )

    assert output["hookSpecificOutput"]["model"] == "gpt-5.6-sol"
    assert "CREDENTIAL RISK" in output["hookSpecificOutput"]["routeMessage"]


def test_hook_surfaces_llm_classifier_confidence_and_audits_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agentroute.classifier.urllib.request.urlopen",
        lambda request, timeout: FakeClassifierResponse(),
    )
    config = default_config()
    config.enabled = True
    config.routing.classifier.enabled = True
    config.routing.classifier.endpoint = "http://127.0.0.1:11434/v1/chat/completions"
    store = AuditStore(tmp_path / "audit.db")

    output = invoke(config, store, "Please handle this")
    row = store.latest("same-thread")

    assert output["hookSpecificOutput"]["model"] == "gpt-5.6-sol"
    assert "classifier confidence 87%" in output["hookSpecificOutput"]["routeMessage"]
    assert "rule score -0.5" in output["hookSpecificOutput"]["routeMessage"]
    assert "implementation" in output["hookSpecificOutput"]["routeMessage"]
    assert row is not None
    assert row["classification_source"] == "local_llm"
    assert row["classifier_task_type"] == "implementation"
    assert len(row["classifier_reason_hash"]) == 64
    assert "substantial implementation" not in str(dict(row)).lower()
    assert row["classifier_latency_ms"] is not None
    assert len(row["classifier_request_hash"]) == 64
    assert json.loads(row["classifier_usage"])["total_tokens"] == 120


def test_llm_context_telemetry_distinguishes_sent_from_inherited(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agentroute.classifier.urllib.request.urlopen",
        lambda request, timeout: FakeClassifierResponse(),
    )
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Verify the production records against the source system.",
                        }
                    ],
                },
            }
        )
        + "\n"
    )
    config = default_config()
    config.enabled = True
    config.routing.classifier.enabled = True
    config.routing.classifier.endpoint = "http://127.0.0.1:11434/v1/chat/completions"
    store = AuditStore(tmp_path / "audit.db")

    invoke(config, store, "ok check?", transcript_path=transcript)
    row = store.latest("same-thread")
    receipt = json.loads(row["selection_receipt"])

    assert row["previous_context_sent"] == 1
    assert row["resolved_task_inherited"] == 0
    assert row["task_context_used"] == 1
    assert receipt["context"] == {
        "previous_context_sent": True,
        "resolved_task_inherited": False,
    }
    assert receipt["classifier"]["request_hash"] == row["classifier_request_hash"]


def test_explicit_confirmation_approves_agent_request(tmp_path):
    transcript = tmp_path / "rollout.jsonl"
    assistant_text = (
        "This needs a stronger review.\n\n"
        "MODEL_REQUEST: SMART\n"
        "MODEL_REQUEST_REASON: The production failure spans several services."
    )
    transcript.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": assistant_text}],
                },
            }
        )
        + "\n"
    )
    config = default_config()
    config.enabled = True
    store = AuditStore(tmp_path / "audit.db")

    output = invoke(config, store, "ok do it", transcript_path=transcript)
    row = store.latest("same-thread")

    assert "AGENT REQUEST APPROVED" in output["hookSpecificOutput"]["routeMessage"]
    assert output["hookSpecificOutput"]["model"] == "gpt-5.6-sol"
    assert row is not None
    assert row["agent_requested_tier"] == "smart"
    assert len(row["agent_request_reason_hash"]) == 64
    assert "production failure" not in str(dict(row)).lower()


def test_hook_fails_open_on_invalid_input(tmp_path):
    sink = io.StringIO()

    assert codex_user_prompt_submit(io.StringIO("{}"), sink) == 0
    assert json.loads(sink.getvalue())["continue"] is True


def test_classifier_fallback_is_visible_in_model_line(tmp_path, monkeypatch):
    def unavailable(request, timeout):
        raise TimeoutError("classifier timed out")

    monkeypatch.setattr("agentroute.classifier.urllib.request.urlopen", unavailable)
    config = default_config()
    config.enabled = True
    config.routing.classifier.enabled = True
    config.routing.classifier.endpoint = "http://127.0.0.1:11434/v1/chat/completions"

    output = invoke(config, AuditStore(tmp_path / "audit.db"), "Please handle this")

    assert "CLASSIFIER FALLBACK" in output["hookSpecificOutput"]["routeMessage"]


def test_stop_hook_records_exact_turn_usage(tmp_path):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "token_usage_record",
                "payload": {
                    "turn_id": "turn-1",
                    "turn_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 800,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 50,
                        "reasoning_output_tokens": 10,
                        "total_tokens": 1050,
                    },
                },
            }
        )
        + "\n"
    )
    config = default_config()
    store = AuditStore(tmp_path / "audit.db")
    invoke(config, store, "show status")
    sink = io.StringIO()
    payload = {
        "session_id": "same-thread",
        "turn_id": "turn-1",
        "model": "gpt-5.6-luna",
        "transcript_path": str(transcript),
    }

    assert codex_stop(io.StringIO(json.dumps(payload)), sink, store=store) == 0
    row = store.latest("same-thread")

    assert json.loads(sink.getvalue()) == {"continue": True, "suppressOutput": True}
    assert row["answer_model"] == "gpt-5.6-luna"
    assert row["answer_input_tokens"] == 1000
    assert row["answer_cached_input_tokens"] == 800
    assert row["answer_output_tokens"] == 50


def test_stop_hook_records_completion_when_usage_is_missing(tmp_path):
    config = default_config()
    store = AuditStore(tmp_path / "audit.db")
    invoke(config, store, "show status")
    sink = io.StringIO()
    payload = {
        "session_id": "same-thread",
        "turn_id": "turn-1",
        "model": "gpt-5.6-luna",
        "transcript_path": str(tmp_path / "missing.jsonl"),
    }

    assert codex_stop(io.StringIO(json.dumps(payload)), sink, store=store) == 0
    row = store.latest("same-thread")

    assert row["turn_completed_at"] is not None
    assert row["turn_duration_ms"] >= 0
    assert row["usage_recorded_at"] is None


def test_subagent_stop_records_completion_for_flat_codex_fields(tmp_path):
    config = default_config()
    store = AuditStore(tmp_path / "audit.db")
    invoke(
        config,
        store,
        "Design a zero-downtime database migration",
        flat_agent_id="0199-child",
        flat_agent_type="worker",
    )
    sink = io.StringIO()
    payload = {
        "session_id": "same-thread",
        "turn_id": "turn-1",
        "model": "gpt-5.6-luna",
        "agent_id": "0199-child",
        "agent_type": "worker",
        "agent_transcript_path": str(tmp_path / "missing-child.jsonl"),
    }

    assert codex_stop(io.StringIO(json.dumps(payload)), sink, store=store) == 0
    row = store.latest("same-thread")

    assert row["route_scope"] == "subagent"
    assert row["agent_id"] == "0199-child"
    assert row["turn_outcome"] == "completed"
    assert row["usage_status"] == "missing"


def test_subagent_stop_reads_usage_from_child_transcript(tmp_path):
    parent_transcript = tmp_path / "parent.jsonl"
    child_transcript = tmp_path / "child.jsonl"
    parent_transcript.write_text(
        json.dumps(
            {
                "type": "token_usage_record",
                "payload": {
                    "turn_id": "turn-1",
                    "turn_token_usage": {"input_tokens": 9999, "total_tokens": 9999},
                },
            }
        )
        + "\n"
    )
    child_transcript.write_text(
        json.dumps(
            {
                "type": "token_usage_record",
                "payload": {
                    "turn_id": "turn-1",
                    "turn_token_usage": {
                        "input_tokens": 321,
                        "cached_input_tokens": 123,
                        "output_tokens": 45,
                        "reasoning_output_tokens": 6,
                        "total_tokens": 366,
                    },
                },
            }
        )
        + "\n"
    )
    config = default_config()
    store = AuditStore(tmp_path / "audit.db")
    invoke(
        config,
        store,
        "Investigate the distributed-system failure",
        flat_agent_id="0199-child",
        flat_agent_type="worker",
    )
    sink = io.StringIO()
    payload = {
        "session_id": "same-thread",
        "turn_id": "turn-1",
        "model": "gpt-5.6-sol",
        "agent_id": "0199-child",
        "agent_type": "worker",
        "transcript_path": str(parent_transcript),
        "agent_transcript_path": str(child_transcript),
    }

    assert codex_stop(io.StringIO(json.dumps(payload)), sink, store=store) == 0
    row = store.latest("same-thread")

    assert row["route_scope"] == "subagent"
    assert row["agent_id"] == "0199-child"
    assert row["answer_input_tokens"] == 321
    assert row["answer_cached_input_tokens"] == 123
    assert row["answer_output_tokens"] == 45
    assert row["answer_reasoning_output_tokens"] == 6
    assert row["usage_status"] == "recorded"
