from copy import deepcopy

import pytest

from agentroute.capacity import CapacityState
from agentroute.config import ModelCapabilities, ProviderSelectionConfig, default_config
from agentroute.models import ReasonCode, RouteContext, Tier
from agentroute.provider_selection import select_provider
from agentroute.router import Router


@pytest.fixture
def setup_policy(monkeypatch):
    config = default_config()
    config.routing.provider_selection = ProviderSelectionConfig(mode="adaptive", cost_weight=0)
    for name in ("claude", "grok"):
        config.backends[name] = deepcopy(config.backends["deepseek"])
        config.backends[name].api_key_env = None
        config.backends[name].codex_provider = f"agentroute-{name}"
    for backend in config.backends.values():
        backend.enabled = True
    monkeypatch.setattr("agentroute.provider_selection.backend_readiness", lambda *_: (True, []))
    monkeypatch.setattr("agentroute.provider_selection.local_bridge_ready", lambda *_: True)
    monkeypatch.setattr("agentroute.router.backend_readiness", lambda *_: (True, []))
    states = {
        name: CapacityState(name, "healthy", "test", used_percent=10) for name in config.backends
    }
    monkeypatch.setattr(
        "agentroute.provider_selection.candidate_capacity", lambda _c, _ctx, name: states[name]
    )
    return config, states


def context(**kwargs):
    return RouteContext(
        session_id="selection-test", **{"latest_prompt": "Implement this", **kwargs}
    )


def pick(config, task="implementation", **kwargs):
    return select_provider(config, context(**kwargs), Tier.SMART, task, "gpt")


@pytest.mark.parametrize(
    "task,expected",
    [
        ("implementation", "gpt"),
        ("architecture", "claude"),
        ("research", "grok"),
        ("formatting", "deepseek"),
    ],
)
def test_task_preferences_change_provider(setup_policy, task, expected):
    config, _ = setup_policy
    assert pick(config, task)[0] == expected


def test_usage_pressure_can_outweigh_task_preference(setup_policy):
    config, states = setup_policy
    states["gpt"] = CapacityState("gpt", "warning", "90% used", used_percent=90)
    selected, receipt = pick(config)
    assert selected == "claude"
    assert next(r for r in receipt["candidates"] if r["backend"] == "gpt")["usage_penalty"] > 3


def test_budget_and_unavailable_are_exclusions(setup_policy):
    config, states = setup_policy
    states["gpt"] = CapacityState("gpt", "exhausted", "quota reached", used_percent=99)
    states["claude"] = CapacityState("claude", "budget_reached", "budget reached")
    states["deepseek"] = CapacityState("deepseek", "unavailable", "cooldown")
    selected, receipt = pick(config)
    assert selected == "grok"
    assert sum(r["eligible"] for r in receipt["candidates"]) == 1


def test_budget_headroom_changes_ranking_before_hard_stop(setup_policy):
    config, _ = setup_policy
    config.backends["gpt"].daily_budget_usd = 10
    config.routing.provider_selection.task_preferences["implementation"] = []
    selected, receipt = pick(config, backend_daily_spend={"gpt": 9})
    assert next(r for r in receipt["candidates"] if r["backend"] == "gpt")["budget_fraction"] == 0.9
    # No budget is invented for unconfigured providers.
    assert all(r["budget_fraction"] == 0 for r in receipt["candidates"] if r["backend"] != "gpt")


@pytest.mark.parametrize(
    "lock",
    [
        "explicit_backend",
        "requested_backend",
        "explicit_session_preference",
        "explicit_spawn_model",
    ],
)
def test_explicit_choice_never_runs_candidate_policy(setup_policy, lock):
    config, _ = setup_policy
    selected, receipt = select_provider(
        config, context(), Tier.SMART, "research", "gpt", locked_by=lock
    )
    assert selected == "gpt"
    assert receipt["locked_by"] == lock
    assert receipt["candidates"] == []


def test_shadow_reports_recommendation_without_changing_provider(setup_policy):
    config, _ = setup_policy
    config.routing.provider_selection.mode = "shadow"
    selected, receipt = pick(config, "research")
    assert selected == "gpt"
    assert receipt["recommended_backend"] == "grok"


def test_all_ineligible_blocks_without_fallback_outside_allowlist(setup_policy):
    config, states = setup_policy
    config.routing.provider_selection.candidates = ["claude"]
    states["claude"] = CapacityState("claude", "exhausted", "quota reached")
    decision = Router(config).route(context())
    assert decision.capacity_blocked
    assert decision.capacity_trigger == "provider_selection"
    assert ReasonCode.PROVIDER_SELECTION_BLOCKED in decision.reason_codes
    assert decision.selection_receipt["provider_selection"]["recommended_backend"] is None


def test_switch_penalty_prevents_needless_hopping(setup_policy):
    config, _ = setup_policy
    selected, receipt = pick(
        config, current_model="active", current_model_provider="agentroute-claude"
    )
    assert selected == "claude"  # Exact tie stays with the current provider.
    assert next(r for r in receipt["candidates"] if r["backend"] == "gpt")["switch_penalty"] == 1


def test_context_and_tools_exclude_unsuitable_model(setup_policy):
    config, _ = setup_policy
    model = config.backends["gpt"].target(Tier.SMART).model
    config.capabilities.models[model] = ModelCapabilities(context_window=100, tool_calling="none")
    selected, receipt = pick(config, previous_response_usage={"input_tokens": 200})
    row = next(r for r in receipt["candidates"] if r["backend"] == "gpt")
    assert selected != "gpt"
    assert set(row["exclusions"]) == {"context_window_too_small", "tool_calling_unavailable"}


def test_disabled_and_unknown_providers_are_not_admitted(setup_policy, monkeypatch):
    config, _ = setup_policy
    config.routing.provider_selection.candidates = ["missing", "claude", "gpt"]
    monkeypatch.setattr(
        "agentroute.provider_selection.backend_readiness",
        lambda _c, n: (n != "claude", ["disabled"] if n == "claude" else []),
    )
    selected, receipt = pick(config)
    assert selected == "gpt"
    assert receipt["candidates"][0]["exclusions"] == ["not_configured"]
    assert receipt["candidates"][1]["exclusions"] == ["disabled"]


def test_router_selects_provider_without_persisting_preference(setup_policy):
    config, _ = setup_policy
    decision = Router(config).route(context(latest_prompt="Design the cross-cutting architecture"))
    assert decision.backend == "claude"
    assert decision.sticky_backend is None
    assert ReasonCode.PROVIDER_SELECTION in decision.reason_codes


def test_router_preserves_explicit_session_route(setup_policy):
    config, _ = setup_policy
    decision = Router(config).route(context(sticky_backend="gpt"))
    assert decision.backend == "gpt"
    assert (
        decision.selection_receipt["provider_selection"]["locked_by"]
        == "explicit_session_preference"
    )


def test_automatic_subagent_inheritance_is_soft(setup_policy):
    config, _ = setup_policy
    decision = Router(config).route(
        context(
            latest_prompt="Design the cross-cutting architecture",
            inherited_backend="grok",
            route_scope="subagent",
        )
    )
    assert decision.backend == "claude"
    assert decision.sticky_backend is None


def test_static_is_default_and_does_not_change_routing():
    config = default_config()
    assert config.routing.provider_selection.mode == "static"
    selected, receipt = select_provider(config, context(), Tier.SMART, "research", "gpt")
    assert selected == "gpt"
    assert not receipt["candidates"]


def test_shadow_preserves_actual_automatic_child_inheritance(setup_policy):
    config, _ = setup_policy
    config.routing.provider_selection.mode = "shadow"
    decision = Router(config).route(context(inherited_backend="deepseek", route_scope="subagent"))
    assert decision.backend == "deepseek"
    assert decision.selection_receipt["provider_selection"]["selected_backend"] == "deepseek"


def test_unknown_context_limit_does_not_admit_a_switch_with_measured_history(setup_policy):
    config, _ = setup_policy
    target = config.backends["gpt"].target(Tier.SMART)
    config.capabilities.models[target.model] = ModelCapabilities(tool_calling="full")
    _, receipt = pick(config, previous_response_usage={"input_tokens": 200})
    row = next(r for r in receipt["candidates"] if r["backend"] == "gpt")
    assert "context_window_unverified_for_switch" in row["exclusions"]
    assert not row["prior_input_fits"]


def test_revalidated_capacity_cannot_escape_adaptive_allowlist(setup_policy, monkeypatch):
    config, _ = setup_policy
    config.routing.provider_selection.candidates = ["gpt"]
    config.capacity.enabled = True
    monkeypatch.setattr("agentroute.router.backend_state", lambda *a, **kw: CapacityState(
        "gpt", "exhausted", "quota changed after selection", "subscription",
    ))
    decision = Router(config).route(context())
    assert decision.capacity_blocked
    assert decision.backend == "gpt"


def test_claude_pool_missing_sample_is_unknown_not_zero(monkeypatch):
    from agentroute.provider_selection import _claude_capacity

    config = default_config()
    monkeypatch.setattr("agentroute.claude_profiles.profile_account_identity", lambda _: "id")
    monkeypatch.setattr("agentroute.claude_profile_pool.ClaudeProfilePool._recorded_percent",
                        lambda *a: None)
    state = _claude_capacity(config, CapacityState("claude", "healthy", "budget okay"))
    assert state.status == "unknown"
    assert state.used_percent is None


def test_candidate_cannot_bypass_its_backend_specific_risk_floor(setup_policy):
    config, _ = setup_policy
    config.policy.backend_risk_floors = {"deepseek": {"security": "max"}}
    config.routing.provider_selection.candidates = ["deepseek"]
    decision = Router(config).route(context(latest_prompt="Review security of this change"))
    assert decision.capacity_blocked
    rows = decision.selection_receipt["provider_selection"]["candidates"]
    assert "backend_risk_floor:max" in rows[0]["exclusions"]
    manual = Router(config).route(context(latest_prompt="@smart Review security of this change"))
    assert manual.tier is Tier.SMART
    assert not manual.capacity_blocked
