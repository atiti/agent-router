from agentroute.config import default_config
from agentroute.models import ReasonCode, RouteContext, Tier
from agentroute.router import Router, confidence_for


def route(prompt: str, **kwargs):
    return Router(default_config()).route(
        RouteContext(session_id="test-session", latest_prompt=prompt, **kwargs)
    )


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
