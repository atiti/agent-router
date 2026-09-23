from agentroute.classifier import ClassifierResult
from agentroute.config import ExecutionBackendConfig, ModelTarget, default_config
from agentroute.models import ReasonCode, RouteContext, Tier
from agentroute.router import Router, confidence_for


def route(prompt: str, **kwargs):
    return Router(default_config()).route(
        RouteContext(session_id="test-session", latest_prompt=prompt, **kwargs)
    )


class FakeClassifier:
    source = "cloud_llm"

    def __init__(
        self,
        tier=Tier.SMART,
        confidence=0.84,
        reasoning_effort="high",
        task_type="implementation",
        source="cloud_llm",
    ):
        self.source = source
        self.tier = tier
        self.confidence = confidence
        self.reasoning_effort = reasoning_effort
        self.task_type = task_type
        self.calls = 0

    def classify(self, context):
        self.calls += 1
        return ClassifierResult(
            tier=self.tier,
            reasoning_effort=self.reasoning_effort,
            confidence=self.confidence,
            task_type=self.task_type,
            reason="A stronger model is likely to improve task completion.",
        )


class BrokenClassifier:
    source = "cloud_llm"

    def classify(self, context):
        raise TimeoutError("classifier timed out")


def hybrid_route(prompt: str, classifier, **kwargs):
    config = default_config()
    config.routing.classifier.enabled = True
    return Router(config, classifier=classifier).route(
        RouteContext(session_id="test-session", latest_prompt=prompt, **kwargs)
    )


def test_public_classifier_default_is_gpt_luna():
    assert default_config().routing.classifier.model == "gpt-5.6-luna"


def test_luna_minimum_reasoning_is_xhigh_for_gpt_and_azure(monkeypatch):
    config = default_config()
    assert config.routing.classifier.reasoning_effort == "low"
    config.backends["gpt"].tiers["fast"].reasoning_effort = "low"
    gpt = Router(config).route(
        RouteContext(
            session_id="gpt-luna-floor",
            latest_prompt="@gpt @fast @low say hello",
            current_tier=Tier.NORMAL,
        )
    )

    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    config.backends["azure"].tiers["fast"] = ModelTarget(
        model="dev-gpt-6-luna", reasoning_effort="low"
    )
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    azure = Router(config).route(
        RouteContext(
            session_id="azure-luna-floor",
            latest_prompt="@azure @fast @low say hello",
            current_tier=Tier.NORMAL,
        )
    )

    for decision in (gpt, azure):
        assert decision.reasoning_effort == "xhigh"
        assert decision.reasoning_effort_source == "model_floor"
        assert ReasonCode.MODEL_REASONING_FLOOR in decision.reason_codes
        assert decision.selection_receipt["policy"][
            "minimum_model_reasoning_effort"
        ] == "xhigh"


def test_luna_reasoning_floor_does_not_change_other_models():
    config = default_config()
    config.backends["gpt"].tiers["fast"].model = "gpt-6-sol"
    config.backends["gpt"].tiers["fast"].reasoning_effort = "low"
    decision = Router(config).route(
        RouteContext(
            session_id="sol-no-luna-floor",
            latest_prompt="@gpt @fast @low say hello",
            current_tier=Tier.NORMAL,
        )
    )

    assert decision.model == "gpt-6-sol"
    assert decision.reasoning_effort == "low"
    assert decision.reasoning_effort_source == "manual"
    assert ReasonCode.MODEL_REASONING_FLOOR not in decision.reason_codes


def test_hybrid_uses_llm_for_ambiguous_prompt():
    classifier = FakeClassifier()
    decision = hybrid_route("Please handle this", classifier, current_tier=Tier.NORMAL)

    assert decision.tier is Tier.SMART
    assert decision.classification_source == "cloud_llm"
    assert decision.classifier_confidence == 0.84
    assert decision.classifier_task_type == "implementation"
    assert decision.classifier_reasoning_effort == "high"
    assert decision.reasoning_effort == "high"
    assert decision.reasoning_effort_source == "classifier"
    assert len(decision.classifier_reason_hash or "") == 64
    assert ReasonCode.LLM_CLASSIFIER in decision.reason_codes
    assert len(decision.selection_receipt_hash or "") == 64
    assert decision.selection_receipt["classifier"]["model"] == "gpt-5.6-luna"
    candidates = decision.selection_receipt["candidates"]
    assert next(item for item in candidates if item["tier"] == "max")["eligible"] is False


def test_hybrid_skips_llm_for_high_confidence_rule():
    classifier = FakeClassifier()
    decision = hybrid_route("any outstanding commits?", classifier, current_tier=Tier.NORMAL)

    assert decision.tier is Tier.FAST
    assert classifier.calls == 0
    assert decision.classification_source == "heuristic"


def test_hybrid_never_sends_credential_shaped_prompt_to_llm():
    classifier = FakeClassifier(tier=Tier.FAST)
    decision = hybrid_route(
        "service api key: abcdefghijklmnop1234", classifier, current_tier=Tier.NORMAL
    )

    assert decision.tier is Tier.SMART
    assert classifier.calls == 0
    assert decision.classification_source == "heuristic_sensitive"


def test_hybrid_never_sends_credential_from_assistant_context_to_llm():
    classifier = FakeClassifier(tier=Tier.FAST)
    decision = hybrid_route(
        "Please handle this",
        classifier,
        current_tier=Tier.NORMAL,
        task_definition="Use api key: abcdefghijklmnop1234 to finish the task.",
    )

    assert classifier.calls == 0
    assert decision.classification_source == "heuristic_sensitive"


def test_hybrid_fails_back_to_heuristic():
    classifier = BrokenClassifier()
    decision = hybrid_route("Please handle this", classifier, current_tier=Tier.NORMAL)

    assert decision.tier is Tier.NORMAL
    assert decision.classification_source == "heuristic_fallback"
    assert decision.classifier_status == "error"
    assert decision.classifier_error_type == "TimeoutError"
    assert decision.selection_receipt["classifier"]["model"] == "gpt-5.6-luna"
    assert ReasonCode.CLASSIFIER_FALLBACK in decision.reason_codes


def test_low_confidence_jev_uses_local_llm_fallback_and_audits_both_steps():
    config = default_config()
    config.routing.classifier.enabled = True
    config.routing.classifier.engine = "jev"
    config.routing.mode = "llm"
    config.routing.classifier.jev_shadow.acceptance_threshold = 0.55
    decision = Router(
        config,
        classifier=FakeClassifier(
            tier=Tier.SMART,
            confidence=0.20,
            reasoning_effort="high",
            task_type="debugging",
            source="local_jev",
        ),
        fallback_classifier=FakeClassifier(
            tier=Tier.SMART,
            confidence=0.84,
            reasoning_effort="high",
            task_type="debugging",
            source="local_llm",
        ),
    ).route(RouteContext(session_id="test-session", latest_prompt="Please handle this"))

    assert decision.tier is Tier.SMART
    assert decision.reasoning_effort == "high"
    assert decision.reasoning_effort_source == "classifier"
    assert decision.classification_source == "local_llm_fallback"
    assert decision.classifier_confidence == 0.84
    assert decision.classifier_task_type == "debugging"
    assert ReasonCode.JEV_LOW_CONFIDENCE in decision.reason_codes
    assert ReasonCode.LLM_CLASSIFIER in decision.reason_codes
    assert decision.selection_receipt["classifier"]["status"] == "succeeded"
    assert decision.selection_receipt["jev_first_pass"]["tier_signals"] == {}


def test_high_confidence_fast_rule_bypasses_jev():
    config = default_config()
    config.routing.classifier.enabled = True
    config.routing.classifier.engine = "jev"
    config.routing.mode = "llm"
    classifier = FakeClassifier(source="local_jev")

    decision = Router(config, classifier=classifier).route(
        RouteContext(session_id="test-session", latest_prompt="is it working?")
    )

    assert decision.tier is Tier.FAST
    assert decision.classification_source == "heuristic"
    assert classifier.calls == 0


def test_confirmation_with_appended_question_uses_previous_task():
    decision = hybrid_route(
        "ok do it. should it run locally?",
        FakeClassifier(),
        current_tier=Tier.NORMAL,
        task_definition="Implement a multi-step model-routing architecture end to end.",
    )

    assert decision.tier is Tier.SMART
    assert decision.task_context_used
    assert ReasonCode.TASK_DEFINITION_INHERITANCE in decision.reason_codes


def test_mechanical_task_downgrades_to_fast():
    decision = route("Rename the label and fix the typo", current_tier=Tier.NORMAL)

    assert decision.tier is Tier.FAST
    assert decision.model == "gpt-6-luna"
    assert ReasonCode.MECHANICAL_TASK in decision.reason_codes


def test_reasoning_effort_prefix_preserves_tier_and_is_audited():
    decision = route("@ultra rename the label", current_tier=Tier.NORMAL)

    assert decision.tier is Tier.FAST
    assert decision.reasoning_effort == "ultra"
    assert decision.requested_reasoning_effort == "ultra"
    assert ReasonCode.REASONING_EFFORT_OVERRIDE in decision.reason_codes
    assert decision.selection_receipt["policy"]["reasoning_effort_override"] == "ultra"


def test_read_only_issue_comment_retrieval_routes_to_fast():
    decision = route(
        "pull the latest comment from the issue owner: "
        "https://github.com/example/project/issues/1226#issuecomment-5678199421",
        current_tier=Tier.FAST,
    )

    assert decision.tier is Tier.FAST
    assert decision.raw_score == -2
    assert ReasonCode.READ_ONLY_RETRIEVAL in decision.reason_codes


def test_simple_existing_context_question_routes_to_fast():
    decision = route("didn't we tell him how to do that already?", current_tier=Tier.NORMAL)

    assert decision.tier is Tier.FAST
    assert decision.raw_score == -1.5
    assert ReasonCode.SIMPLE_CONTEXT_QUESTION in decision.reason_codes


def test_read_only_status_routes_to_fast():
    decision = route("any outstanding commits?", current_tier=Tier.NORMAL)

    assert decision.tier is Tier.FAST
    assert decision.confidence >= 0.9
    assert ReasonCode.READ_ONLY_STATUS in decision.reason_codes


def test_resolved_status_question_routes_to_fast():
    decision = route("is the api key issue solved?", current_tier=Tier.NORMAL)

    assert decision.tier is Tier.FAST
    assert ReasonCode.READ_ONLY_STATUS in decision.reason_codes


def test_credential_value_has_smart_risk_floor():
    decision = route("service api key: abcdefghijklmnop1234", current_tier=Tier.FAST)

    assert decision.tier >= Tier.SMART
    assert decision.confidence >= 0.9
    assert ReasonCode.CREDENTIAL_EXPOSURE in decision.reason_codes


def test_operational_incident_has_smart_risk_floor():
    decision = route("why is the comms runtime still flapping?", current_tier=Tier.NORMAL)

    assert decision.tier >= Tier.SMART
    assert ReasonCode.OPERATIONAL_INCIDENT in decision.reason_codes


def test_retrieval_wording_does_not_lower_risky_analysis():
    decision = route(
        "Open this issue and redesign the authentication architecture",
        current_tier=Tier.NORMAL,
    )

    assert decision.tier >= Tier.SMART


def test_confidence_uses_the_actual_max_boundary():
    assert confidence_for(5.5, Tier.MAX, 1) == 0.62


def test_architecture_and_migration_proposes_max_with_safe_fallback():
    decision = route(
        "Redesign the architecture for a zero-downtime database migration",
        current_tier=Tier.NORMAL,
    )

    assert decision.proposed_tier is Tier.MAX
    assert decision.tier is Tier.SMART
    assert decision.model == "gpt-6-sol"


def test_security_has_smart_risk_floor():
    decision = route("Fix this authentication permission", current_tier=Tier.NORMAL)

    assert decision.tier >= Tier.SMART


def test_confirmation_inherits_previous_task_tier():
    decision = route(
        "go ahead",
        current_tier=Tier.NORMAL,
        previous_task_tier=Tier.SMART,
    )

    assert decision.tier is Tier.SMART
    assert decision.inherited
    assert ReasonCode.PREVIOUS_TASK_INHERITANCE in decision.reason_codes


def test_bounded_slack_reply_routes_normal_without_inheriting_completed_work():
    config = default_config()
    config.routing.classifier.enabled = True
    classifier = FakeClassifier(
        tier=Tier.NORMAL, confidence=0.95, reasoning_effort="medium", task_type="communication"
    )
    decision = Router(config, classifier=classifier).route(
        RouteContext(
            session_id="reply",
            latest_prompt="reply to the project owner in the Slack thread",
            current_tier=Tier.SMART,
            previous_task_tier=Tier.SMART,
            task_definition="Implemented and merged a complex cross-repository production fix.",
        )
    )

    assert decision.tier is Tier.NORMAL
    assert decision.reasoning_effort == "xhigh"
    assert decision.reasoning_effort_source == "model_floor"
    assert ReasonCode.MODEL_REASONING_FLOOR in decision.reason_codes
    assert classifier.calls == 1
    assert ReasonCode.BOUNDED_COMMUNICATION in decision.reason_codes


def test_classifier_fast_is_floored_for_judgment_work():
    decision = hybrid_route(
        "write the project update",
        FakeClassifier(tier=Tier.FAST, reasoning_effort="medium", task_type="communication"),
        current_tier=Tier.NORMAL,
    )

    assert decision.tier is Tier.NORMAL
    assert decision.reasoning_effort == "xhigh"
    assert decision.reasoning_effort_source == "model_floor"
    assert ReasonCode.MODEL_REASONING_FLOOR in decision.reason_codes
    assert ReasonCode.FAST_QUALITY_FLOOR in decision.reason_codes


def test_classifier_fast_remains_available_for_exact_retrieval():
    decision = hybrid_route(
        "find this exact identifier",
        FakeClassifier(
            tier=Tier.FAST, confidence=0.95, reasoning_effort="low", task_type="retrieval"
        ),
        current_tier=Tier.NORMAL,
    )

    assert decision.tier is Tier.FAST
    assert decision.reasoning_effort == "xhigh"
    assert decision.reasoning_effort_source == "model_floor"
    assert ReasonCode.FAST_QUALITY_FLOOR not in decision.reason_codes
    assert ReasonCode.MODEL_REASONING_FLOOR in decision.reason_codes


def test_continuation_preserves_previous_tier_floor():
    decision = hybrid_route(
        "continue",
        FakeClassifier(tier=Tier.FAST, reasoning_effort="low", task_type="acknowledgment"),
        current_tier=Tier.SMART,
        previous_task_tier=Tier.SMART,
        previous_reasoning_effort="xhigh",
        task_definition="Implement and verify the cross-provider routing architecture.",
    )

    assert decision.tier is Tier.SMART
    assert decision.reasoning_effort == "xhigh"
    assert ReasonCode.CONTINUATION_CAPABILITY_FLOOR in decision.reason_codes


def test_confirmation_uses_assistant_defined_task_scope():
    decision = route(
        "ok do it",
        current_tier=Tier.NORMAL,
        previous_task_tier=Tier.NORMAL,
        task_definition=(
            "Redesign the authentication architecture and execute a zero-downtime migration."
        ),
    )

    assert decision.proposed_tier is Tier.MAX
    assert decision.tier is Tier.SMART
    assert decision.task_context_used
    assert ReasonCode.TASK_DEFINITION_INHERITANCE in decision.reason_codes
    assert ReasonCode.MODEL_COMPATIBILITY_FALLBACK in decision.reason_codes


def test_user_confirmation_approves_agent_model_request():
    decision = route(
        "ok do it",
        current_tier=Tier.NORMAL,
        previous_task_tier=Tier.NORMAL,
        task_definition="This routine task needs a stronger review.",
        agent_requested_tier=Tier.SMART,
        agent_request_reason_hash="a" * 64,
    )

    assert decision.tier is Tier.SMART
    assert decision.confidence == 0.99
    assert decision.agent_requested_tier is Tier.SMART
    assert ReasonCode.AGENT_ESCALATION in decision.reason_codes


def test_risk_floor_can_override_approved_agent_downgrade():
    decision = route(
        "ok",
        current_tier=Tier.SMART,
        task_definition="service api key: abcdefghijklmnop1234",
        agent_requested_tier=Tier.FAST,
    )

    assert decision.tier is Tier.SMART
    assert decision.risk_floor_applied
    assert ReasonCode.AGENT_ESCALATION in decision.reason_codes


def test_manual_max_still_honors_explicit_escape_hatch():
    decision = route("@max do the task", current_tier=Tier.SMART)

    assert decision.tier is Tier.MAX
    assert ReasonCode.MODEL_COMPATIBILITY_FALLBACK not in decision.reason_codes


def test_switched_compares_with_previous_selected_route():
    decision = route(
        "debug why does this fail",
        current_tier=Tier.FAST,
        previous_task_tier=Tier.NORMAL,
    )

    assert decision.tier is Tier.NORMAL
    assert not decision.switched


def test_manual_override_bypasses_automatic_risk_floor():
    decision = route("@fast review this cryptography code", current_tier=Tier.MAX)

    assert decision.tier is Tier.FAST
    assert decision.confidence == 1
    assert decision.manual_override


def test_policy_max_tier_still_caps_manual_override():
    config = default_config()
    config.policy.max_tier = "smart"
    decision = Router(config).route(
        RouteContext(session_id="test-session", latest_prompt="@max do it")
    )

    assert decision.tier is Tier.SMART
    assert ReasonCode.QUOTA_LIMIT in decision.reason_codes


def test_explicit_deepseek_backend_selects_its_model_and_provider(monkeypatch):
    config = default_config()
    config.backends["deepseek"].enabled = True
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    decision = Router(config).route(
        RouteContext(
            session_id="test-session",
            latest_prompt="@deepseek @smart investigate this failure",
            current_tier=Tier.NORMAL,
        )
    )

    assert decision.backend == "deepseek"
    assert decision.model_provider == "agentroute-deepseek"
    assert decision.model == "deepseek-flash"
    assert ReasonCode.BACKEND_OVERRIDE in decision.reason_codes


def test_explicit_custom_backend_selects_its_model_and_provider():
    config = default_config()
    config.backends["ollama"] = ExecutionBackendConfig(
        enabled=True,
        codex_provider="agentroute-ollama",
        display_name="Local Ollama",
        base_url="http://127.0.0.1:11434/v1",
        tool_compatibility="functions_and_apply_patch",
        tiers={
            "fast": ModelTarget(model="qwen3-coder", reasoning_effort="low"),
            "normal": ModelTarget(model="qwen3-coder", reasoning_effort="medium"),
            "smart": ModelTarget(model="qwen3-coder", reasoning_effort="high"),
            "max": ModelTarget(model="qwen3-coder", reasoning_effort="high"),
        },
    )

    decision = Router(config).route(
        RouteContext(
            session_id="test-session",
            latest_prompt="@ollama @smart investigate this failure",
            current_tier=Tier.NORMAL,
        )
    )

    assert decision.backend == "ollama"
    assert decision.model_provider == "agentroute-ollama"
    assert decision.model == "qwen3-coder"
    assert ReasonCode.BACKEND_OVERRIDE in decision.reason_codes


def test_sticky_backend_keeps_provider_while_tier_can_change(monkeypatch):
    config = default_config()
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    decision = Router(config).route(
        RouteContext(
            session_id="test-session",
            latest_prompt="any outstanding commits?",
            sticky_backend="azure",
            current_tier=Tier.SMART,
        )
    )

    assert decision.tier is Tier.FAST
    assert decision.backend == "azure"
    assert decision.model_provider == "agentroute-azure"
    assert decision.sticky_backend == "azure"
    assert ReasonCode.SESSION_AFFINITY in decision.reason_codes


def test_disabled_backend_fails_visibly_to_gpt():
    decision = route("@azure rename the label", current_tier=Tier.NORMAL)

    assert decision.backend == "gpt"
    assert decision.model_provider == "openai"
    assert ReasonCode.BACKEND_FALLBACK in decision.reason_codes


def test_backend_missing_credential_fails_visibly_to_gpt(tmp_path, monkeypatch):
    config = default_config()
    config.backends["deepseek"].enabled = True
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv(
        "AGENTROUTE_CREDENTIALS_FILE", str(tmp_path / "missing-credentials.env")
    )

    decision = Router(config).route(
        RouteContext(
            session_id="test-session",
            latest_prompt="@deepseek @smart investigate this failure",
        )
    )

    assert decision.backend == "gpt"
    assert decision.model_provider == "openai"
    assert ReasonCode.BACKEND_FALLBACK in decision.reason_codes
