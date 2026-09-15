from agentroute.classifier import ClassifierResult
from agentroute.config import default_config
from agentroute.models import ReasonCode, RouteContext, Tier
from agentroute.router import Router, confidence_for


def route(prompt: str, **kwargs):
    return Router(default_config()).route(
        RouteContext(session_id="test-session", latest_prompt=prompt, **kwargs)
    )


class FakeClassifier:
    source = "cloud_llm"

    def __init__(self, tier=Tier.SMART, confidence=0.84):
        self.tier = tier
        self.confidence = confidence
        self.calls = 0

    def classify(self, context):
        self.calls += 1
        return ClassifierResult(
            tier=self.tier,
            confidence=self.confidence,
            task_type="implementation",
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


def test_hybrid_uses_llm_for_ambiguous_prompt():
    classifier = FakeClassifier()
    decision = hybrid_route("Please handle this", classifier, current_tier=Tier.NORMAL)

    assert decision.tier is Tier.SMART
    assert decision.classification_source == "cloud_llm"
    assert decision.classifier_confidence == 0.84
    assert decision.classifier_task_type == "implementation"
    assert len(decision.classifier_reason_hash or "") == 64
    assert ReasonCode.LLM_CLASSIFIER in decision.reason_codes
    assert len(decision.selection_receipt_hash or "") == 64
    assert decision.selection_receipt["classifier"]["model"] == "gpt-5-mini"
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
    decision = hybrid_route(
        "Please handle this", BrokenClassifier(), current_tier=Tier.NORMAL
    )

    assert decision.tier is Tier.NORMAL
    assert decision.classification_source == "heuristic_fallback"
    assert ReasonCode.CLASSIFIER_FALLBACK in decision.reason_codes


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
    assert decision.model == "gpt-5.6-luna"
    assert ReasonCode.MECHANICAL_TASK in decision.reason_codes


def test_read_only_issue_comment_retrieval_routes_to_fast():
    decision = route(
        "pull latest comment from tamas: "
        "https://github.com/markster-exec/project-tracker/issues/1226#issuecomment-5678199421",
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
    assert decision.model == "gpt-5.6-sol"


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
