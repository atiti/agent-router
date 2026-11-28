"""Explicit YOLO tier: compatibility, opt-in and admission (offline acceptance rows)."""

import pytest
import yaml
from pydantic import ValidationError

from agentroute.classifier import ClassifierResult
from agentroute.config import (
    AppConfig,
    PolicyConfig,
    TierUnavailableError,
    default_config,
    load_config,
)
from agentroute.launcher import progress_review_route_args
from agentroute.model_selection import DirectModelError
from agentroute.models import ORDINARY_TIERS, ReasonCode, RouteContext, Tier
from agentroute.router import Router
from agentroute.signals import model_route_overrides
from agentroute.transcript import parse_agent_model_request

BACKENDS = ("gpt", "azure", "claude", "fable", "grok", "deepseek")


class FakeClassifier:
    source = "cloud_llm"

    def __init__(self, tier):
        self.tier = tier

    def classify(self, context):
        return ClassifierResult(
            tier=self.tier,
            reasoning_effort="high",
            confidence=0.99,
            task_type="architecture",
            reason="Pretend the classifier asked for the strongest tier.",
        )


def admitted_config():
    config = default_config()
    config.policy.max_tier = "yolo"
    return config


def route(config, prompt, **kwargs):
    return Router(config).route(RouteContext(session_id="yolo", latest_prompt=prompt, **kwargs))


# --- Existing installation -------------------------------------------------


def test_tier_values_are_append_only():
    assert [int(tier) for tier in ORDINARY_TIERS] == [0, 1, 2, 3]
    assert int(Tier.YOLO) == 4
    assert Tier.parse("YOLO") is Tier.YOLO
    assert Tier.YOLO.automatic() is Tier.MAX
    assert Tier.SMART.automatic() is Tier.SMART


def test_four_tier_config_loads_with_custom_max_and_policy_unchanged(tmp_path):
    legacy = default_config().model_dump(mode="json", exclude_none=True)
    for backend in legacy["backends"].values():
        backend["tiers"].pop("yolo", None)
    legacy["providers"]["codex"]["tiers"].pop("yolo", None)
    legacy["backends"]["gpt"]["tiers"]["max"] = {"model": "gpt-6-astra", "reasoning_effort": "high"}
    legacy["backends"]["azure"]["tiers"]["max"] = {"model": "my-deploy", "reasoning_effort": "high"}
    legacy["policy"]["risk_floors"]["cryptography"] = "smart"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(legacy), encoding="utf-8")

    config = load_config(path)

    gpt = config.backends["gpt"]
    assert gpt.tiers["max"].model == "gpt-6-astra"
    assert gpt.tiers["max"].reasoning_effort == "high"
    # The account's Astra name is reused; only the effort is raised.
    assert gpt.tiers["yolo"].model == "gpt-6-astra"
    assert gpt.tiers["yolo"].reasoning_effort == "ultra"
    assert config.backends["azure"].tiers["max"].model == "my-deploy"
    assert config.backends["azure"].tiers["yolo"].model == "gpt-6.2-astra"
    assert "yolo" not in config.backends["deepseek"].tiers
    assert config.policy.max_tier == "max"
    assert config.policy.risk_floors["cryptography"] == "smart"
    for tier in ORDINARY_TIERS:
        assert gpt.target(tier).model == legacy["backends"]["gpt"]["tiers"][str(tier)]["model"]


def test_existing_yolo_mapping_is_never_overwritten(tmp_path):
    payload = default_config().model_dump(mode="json", exclude_none=True)
    payload["backends"]["gpt"]["tiers"]["yolo"] = {
        "model": "gpt-6-astra",
        "reasoning_effort": "high",
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    assert load_config(path).backends["gpt"].tiers["yolo"].reasoning_effort == "high"


def test_registered_bridges_receive_bundled_yolo_targets_only():
    from agentroute.grok_bridge import ensure_backend
    from agentroute.providers import ensure_claude_bridge_backend

    config = default_config()
    ensure_claude_bridge_backend(config, 8090)
    ensure_backend(config)
    assert config.backends["claude"].tiers["yolo"].model == "claude-fable-5-2"
    assert config.backends["claude"].tiers["yolo"].reasoning_effort == "ultra"
    assert config.backends["fable"].tiers["yolo"].reasoning_effort == "ultra"
    assert config.backends["grok"].tiers["yolo"].model == "grok-4.7"
    assert config.backends["grok"].tiers["yolo"].reasoning_effort == "xhigh"
    assert "yolo" not in config.backends["deepseek"].tiers


def test_omitted_yolo_mapping_is_unavailable_not_max():
    backend = default_config().backends["deepseek"]
    with pytest.raises(TierUnavailableError):
        backend.target(Tier.YOLO)


def test_policy_validation():
    assert PolicyConfig(max_tier="YOLO").max_tier == "yolo"
    with pytest.raises(ValidationError):
        PolicyConfig(max_tier="ludicrous")
    with pytest.raises(ValidationError):
        PolicyConfig(risk_floors={"security": "yolo"})
    with pytest.raises(ValidationError):
        PolicyConfig(backend_risk_floors={"claude": {"security": "yolo"}})


def test_shared_model_never_infers_yolo():
    config = default_config()
    assert config.providers["codex"].tier_for_model("gpt-6.2-astra") is Tier.MAX


# --- Explicit selection: tag parsing -----------------------------------------


@pytest.mark.parametrize(
    ("prompt", "backend", "rest"),
    [
        ("@yolo solve this difficult problem", None, "solve this difficult problem"),
        ("@gpt @yolo review this design", "gpt", "review this design"),
        ("@claude @yolo investigate the failure", "claude", "investigate the failure"),
        ("@grok @yolo research this question", "grok", "research this question"),
        ("@azure @yolo use my Astra deployment", "azure", "use my Astra deployment"),
        ("@yolo @azure use my Astra deployment", "azure", "use my Astra deployment"),
        ("@YOLO: shout", None, "shout"),
    ],
)
def test_yolo_tags_parse_deterministically(prompt, backend, rest):
    tier, parsed_backend, effort, model, profile, remaining = model_route_overrides(
        prompt, BACKENDS
    )
    assert (tier, parsed_backend, effort, model, profile, remaining) == (
        "yolo", backend, None, None, None, rest
    )


def test_max_tag_still_selects_max_and_conflicting_tiers_are_rejected():
    assert model_route_overrides("@max return to normal", BACKENDS)[0] == "max"
    assert model_route_overrides("@yolo @high go", BACKENDS)[:3] == ("yolo", None, "high")
    with pytest.raises(DirectModelError):
        model_route_overrides("@yolo @max go", BACKENDS)
    # Not a tag unless it is the whole word.
    assert model_route_overrides("@yolos go", BACKENDS)[0] is None


def test_model_request_accepts_yolo():
    request = parse_agent_model_request(
        "Done.\nMODEL_REQUEST: YOLO\nMODEL_REQUEST_REASON: needs the strongest model"
    )
    assert request is not None and request.tier == "yolo"


# --- Automatic routing cannot introduce YOLO ---------------------------------


def test_classifier_output_is_clamped_to_max():
    assert ClassifierResult.from_payload(
        {"tier": "YOLO", "confidence": 0.9, "task_type": "architecture", "reason": "x"}
    ).tier is Tier.MAX
    config = admitted_config()
    config.routing.classifier.enabled = True
    config.routing.mode = "llm"
    decision = Router(config, classifier=FakeClassifier(Tier.YOLO)).route(
        RouteContext(session_id="yolo", latest_prompt="redesign the whole architecture")
    )
    # The automatic MAX transition may further apply the existing SMART safety fallback.
    assert decision.proposed_tier is Tier.MAX
    assert decision.tier is not Tier.YOLO


def test_risk_floor_and_hysteresis_cannot_carry_yolo_into_a_new_task():
    config = admitted_config()
    decision = route(
        config,
        "rotate the production credentials and audit authentication",
        current_tier=Tier.YOLO,
        previous_task_tier=Tier.YOLO,
        previous_yolo_opt_in="explicit_tag",
    )
    assert decision.tier is not Tier.YOLO
    assert decision.tier <= Tier.MAX


def test_hysteresis_holds_max_not_yolo():
    config = admitted_config()
    decision = route(
        config,
        "hello",
        current_tier=Tier.YOLO,
        previous_response_usage={"input_tokens": 100_000},
    )
    assert decision.tier is not Tier.YOLO


def test_progress_review_never_offers_yolo():
    config = admitted_config()
    config.routing.progress_review_live_switching = True
    args = progress_review_route_args(config, [])
    assert "yolo" not in " ".join(args)


def test_direct_model_never_implies_yolo():
    config = admitted_config()
    config.backends["gpt"].tiers["yolo"].model = "gpt-6.2-astra-preview"
    decision = route(config, "@gpt @model:gpt-6.2-astra-preview hello")
    assert decision.tier is not Tier.YOLO


# --- Admission ----------------------------------------------------------------


def test_yolo_is_blocked_by_default_with_a_visible_reason():
    decision = route(default_config(), "@yolo solve this")
    assert decision.capacity_blocked
    assert decision.capacity_trigger == "yolo_admission"
    assert "policy.max_tier" in (decision.capacity_detail or "")
    assert ReasonCode.YOLO_BLOCKED in decision.reason_codes
    receipt = decision.selection_receipt["yolo"]
    assert receipt["status"] == "blocked"
    assert receipt["admitted"] is False


def test_admitted_yolo_selects_astra_at_ultra():
    decision = route(admitted_config(), "@gpt @yolo review this design")
    assert not decision.capacity_blocked
    assert decision.tier is Tier.YOLO
    assert (decision.backend, decision.model, decision.reasoning_effort) == (
        "gpt", "gpt-6.2-astra", "ultra"
    )
    receipt = decision.selection_receipt
    assert receipt["selected"]["tier"] == "yolo"
    assert receipt["yolo"]["opt_in_source"] == "explicit_tag"
    assert receipt["yolo"]["applied_reasoning_effort"] == "ultra"


def test_explicit_lower_effort_is_recorded_with_the_preset():
    decision = route(admitted_config(), "@gpt @yolo @high review")
    assert decision.reasoning_effort == "high"
    yolo = decision.selection_receipt["yolo"]
    assert (yolo["preset_reasoning_effort"], yolo["requested_reasoning_effort"]) == (
        "ultra", "high"
    )
    assert yolo["applied_reasoning_effort"] == "high"


# --- Continuation and exit ---------------------------------------------------


def continuation(config, prompt, **kwargs):
    return route(
        config,
        prompt,
        current_tier=Tier.YOLO,
        previous_task_tier=Tier.YOLO,
        previous_reasoning_effort="ultra",
        **kwargs,
    )


def test_continuation_keeps_recorded_yolo_opt_in():
    decision = continuation(admitted_config(), "ok continue", previous_yolo_opt_in="explicit_tag")
    assert decision.tier is Tier.YOLO
    assert decision.selection_receipt["yolo"]["opt_in_source"] == "continuation"
    assert ReasonCode.YOLO_OPT_IN in decision.reason_codes


def test_interrupted_recovery_keeps_yolo():
    decision = continuation(
        admitted_config(),
        "actually use the other file",
        previous_yolo_opt_in="explicit_tag",
        interrupted_turn_affinity=True,
        interrupted_backend="gpt",
    )
    assert decision.tier is Tier.YOLO
    assert decision.selection_receipt["yolo"]["opt_in_source"] == "interrupted_turn"


def test_continuation_without_recorded_opt_in_is_clamped():
    decision = continuation(admitted_config(), "ok continue")
    assert decision.tier is Tier.MAX


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [("@max continue", Tier.MAX), ("@smart continue", Tier.SMART), ("@auto continue", None)],
)
def test_explicit_tier_or_auto_exits_yolo(prompt, expected):
    decision = continuation(admitted_config(), prompt, previous_yolo_opt_in="explicit_tag")
    assert decision.tier is not Tier.YOLO
    if expected is not None:
        assert decision.tier is expected


def test_new_independent_task_returns_to_classification():
    decision = continuation(
        admitted_config(), "rename the variable foo to bar", previous_yolo_opt_in="explicit_tag"
    )
    assert decision.tier is not Tier.YOLO


def test_confirmed_model_request_can_opt_in():
    decision = route(admitted_config(), "ok do it", agent_requested_tier=Tier.YOLO)
    assert decision.tier is Tier.YOLO
    assert decision.selection_receipt["yolo"]["opt_in_source"] == "agent_request"


def test_app_config_schema_accepts_yolo_tiers():
    payload = default_config().model_dump(mode="json")
    assert AppConfig.model_validate(payload).backends["gpt"].tiers["yolo"].model
