import pytest

from agentroute.audit import AuditStore
from agentroute.config import default_config
from agentroute.models import RouteContext, Tier
from agentroute.pricing import cost_report, token_cost
from agentroute.router import Router


def test_token_cost_separates_cached_and_uncached_input():
    pricing = default_config().pricing
    cost = token_cost(
        "gpt-5.6-luna",
        {"input_tokens": 1_000_000, "cached_input_tokens": 800_000, "output_tokens": 100_000},
        pricing,
    )
    assert cost == pytest.approx(0.176)


def test_provider_prefixed_model_uses_public_model_price():
    pricing = default_config().pricing
    cost = token_cost(
        "provider/gpt-5.6-luna",
        {"input_tokens": 1_000_000, "output_tokens": 0},
        pricing,
    )
    assert cost == pytest.approx(0.20)


@pytest.mark.parametrize(
    ("model", "input_rate", "cache_write_rate", "cached_rate", "output_rate"),
    [
        # Anthropic's published rates: platform.claude.com/docs/en/about-claude/pricing
        ("claude-haiku-4-5-20251001", 1.0, 1.25, 0.10, 5.0),
        ("claude-sonnet-5", 2.0, 2.50, 0.20, 10.0),
        ("claude-opus-5", 5.0, 6.25, 0.50, 25.0),
        ("claude-opus-5-5", 4.0, 5.00, 0.20, 20.0),
    ],
)
def test_bundled_prices_cover_anthropic_rates(
    model, input_rate, cache_write_rate, cached_rate, output_rate
):
    price = default_config().pricing.models[model]
    assert price.input_per_million == input_rate
    assert price.cache_write_per_million == cache_write_rate
    assert price.cached_input_per_million == cached_rate
    assert price.output_per_million == output_rate


def test_claude_models_are_priced_so_no_backend_warns():
    """Every tier model the bridge serves must resolve to a rate."""
    config = default_config()
    claude = config.backends.get("claude")
    if claude is None:
        pytest.skip("no bundled claude backend")
    unpriced = [
        target.model
        for target in claude.tiers.values()
        if target.model not in config.pricing.models
        and target.model not in config.pricing.aliases
    ]
    assert unpriced == []


def test_token_cost_uses_the_opus_5_5_cache_write_rate():
    pricing = default_config().pricing
    cost = token_cost(
        "claude-opus-5-5",
        {
            "input_tokens": 1_000_000,
            "cached_input_tokens": 500_000,
            "cache_write_input_tokens": 250_000,
            "output_tokens": 1_000,
        },
        pricing,
    )
    expected = (250_000 * 4.0 + 250_000 * 5.0 + 500_000 * 0.20 + 1_000 * 20.0) / 1_000_000
    assert cost == pytest.approx(expected)


def test_report_compares_same_observed_tokens_and_includes_classifier(tmp_path):
    config = default_config()
    store = AuditStore(tmp_path / "audit.db")
    decision = Router(config).route(
        RouteContext(session_id="s", turn_id="t", latest_prompt="show status")
    )
    decision.classifier_usage = {"prompt_tokens": 100, "completion_tokens": 10}
    decision.selection_receipt["classifier"]["model"] = "provider/gpt-5.6-luna"
    store.record(decision, Tier.NORMAL)
    store.record_usage(
        "s",
        "t",
        "gpt-5.6-luna",
        {
            "input_tokens": 1000,
            "cached_input_tokens": 800,
            "output_tokens": 50,
            "total_tokens": 1050,
        },
    )

    report = cost_report(store.history(limit=10), config.pricing, "gpt-6-astra")

    assert report.measured_turns == 1
    assert report.unmeasured_turns == 0
    assert report.input_tokens == 1000
    assert report.cached_input_tokens == 800
    assert report.output_tokens == 50
    assert report.actual_cost == pytest.approx(0.000053)
    assert report.baseline_cost == pytest.approx(0.0053)
    assert report.classifier_cost == pytest.approx(0.000032)
    assert report.net_savings == pytest.approx(0.005215)
    assert report.unpriced_models == ()


def test_report_names_unpriced_backend_models(tmp_path):
    config = default_config()
    store = AuditStore(tmp_path / "audit.db")
    decision = Router(config).route(
        RouteContext(session_id="s", turn_id="t", latest_prompt="show status")
    )
    decision.model = "my-azure-deployment"
    store.record(decision, Tier.NORMAL)
    store.record_usage(
        "s",
        "t",
        "my-azure-deployment",
        {"input_tokens": 100, "output_tokens": 10},
    )

    report = cost_report(store.history(limit=10), config.pricing, "gpt-6-astra")

    assert report.unpriced_models == ("my-azure-deployment",)


def test_report_counts_classifier_cost_before_answer_usage_arrives(tmp_path):
    config = default_config()
    store = AuditStore(tmp_path / "audit.db")
    decision = Router(config).route(
        RouteContext(session_id="s", turn_id="t", latest_prompt="show status")
    )
    decision.classifier_usage = {"prompt_tokens": 100, "completion_tokens": 10}
    decision.selection_receipt["classifier"]["model"] = "gpt-5.6-luna"
    store.record(decision, Tier.NORMAL)

    report = cost_report(store.history(limit=10), config.pricing, "gpt-6-astra")

    assert report.measured_turns == 0
    assert report.unmeasured_turns == 1
    assert report.classifier_cost == pytest.approx(0.000032)



def test_jev_classifier_cost_charges_input_only():
    from agentroute.pricing import token_cost
    config = default_config()
    cost = token_cost("jev-latest", {"input_tokens": 1000000, "output_tokens": 1000000},
                      config.pricing)
    assert cost == 0.042
