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


def test_retrieval_wording_does_not_lower_risky_analysis():
    decision = route(
        "Open this issue and redesign the authentication architecture",
        current_tier=Tier.NORMAL,
    )

    assert decision.tier >= Tier.SMART


def test_confidence_uses_the_actual_max_boundary():
    assert confidence_for(5.5, Tier.MAX, 1) == 0.62


def test_architecture_and_migration_routes_to_max():
    decision = route(
        "Redesign the architecture for a zero-downtime database migration",
        current_tier=Tier.NORMAL,
    )

    assert decision.tier is Tier.MAX
    assert decision.model == "gpt-6-astra"


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
