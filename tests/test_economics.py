from agentroute.config import ModelTarget, default_config
from agentroute.economics import cache_switch_estimate
from agentroute.models import RouteContext, Tier


def setup(backend="azure"):
    config = default_config()
    config.routing.switching.cache_economics = "retain"
    config.backends[backend].enabled = True
    config.backends[backend].tiers["smart"] = ModelTarget(model="gpt-5.6-sol")
    config.backends[backend].tiers["normal"] = ModelTarget(model="gpt-5.6-terra")
    config.routing.backend_by_tier.update(smart=backend, normal=backend)
    current = config.pricing.models["gpt-5.6-sol"]
    candidate = config.pricing.models["gpt-5.6-terra"]
    current.input_per_million = 10
    current.cached_input_per_million = 0.1
    candidate.input_per_million = 5
    context = RouteContext(
        session_id="s",
        latest_prompt="continue",
        current_tier=Tier.SMART,
        current_model="gpt-5.6-sol",
        current_model_provider=config.backends[backend].codex_provider,
        previous_response_usage={
            "input_tokens": 10000,
            "cached_input_tokens": 9900,
            "output_tokens": 0,
        },
    )
    return config, context


def test_api_cache_can_retain_but_shadow_does_not_change_routing():
    config, context = setup()
    assert cache_switch_estimate(config, context, Tier.NORMAL)["retain"]
    config.routing.switching.cache_economics = "shadow"
    result = cache_switch_estimate(config, context, Tier.NORMAL)
    assert result["prefer_stay"] and not result["retain"]


def test_subscription_estimates_never_claim_cash_savings_or_retain():
    config, context = setup("gpt")
    result = cache_switch_estimate(config, context, Tier.NORMAL)
    assert not result["retain"]
    assert result["basis"] == "API-equivalent estimate"


def test_no_downgrade_or_cross_provider_retention():
    config, context = setup()
    for update in (
        {"interrupted_turn_affinity": True},
        {"sticky_backend": "azure"},
        {"current_model_provider": "other"},
        {"inherited_backend": "azure"},
    ):
        assert (
            cache_switch_estimate(config, context.model_copy(update=update), Tier.NORMAL)["status"]
            == "ineligible"
        )
    assert cache_switch_estimate(config, context, Tier.MAX)["status"] == "ineligible"


def test_absent_cache_evidence_never_retains():
    config, context = setup()
    context.previous_response_usage = {}
    assert (
        cache_switch_estimate(config, context, Tier.NORMAL)["status"]
        == "insufficient_cache_evidence"
    )


def test_previous_receipt_requires_recent_completed_same_provider_response():
    import json
    from datetime import datetime, timedelta, timezone

    from agentroute.hook import _previous_response_usage

    row = {
        "actual_model": "model", "answer_model": "model",
        "actual_model_provider": "azure", "model_provider": "azure",
        "turn_completed_at": datetime.now(timezone.utc).isoformat(),
        "turn_outcome": "completed",
        "execution_receipt": json.dumps({"last_response_usage": {"input_tokens": 100}}),
    }
    assert _previous_response_usage(row, "model", "azure") == {"input_tokens": 100}
    assert _previous_response_usage(row, "model", "openai") == {}
    assert _previous_response_usage(row, "other", "azure") == {}
    row["turn_completed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
    assert _previous_response_usage(row, "model", "azure") == {}
