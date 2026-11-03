import json

import pytest

from agentroute.audit import AuditStore
from agentroute.config import ModelCapabilities, ModelTarget, _with_default_backends, default_config
from agentroute.models import RouteContext, Tier
from agentroute.pricing import cost_report, token_cost, token_cost_upper_bound, turn_cost
from agentroute.router import Router


def request_cost_row(requests, *, coverage="turn_start_seen"):
    keys = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")
    row = {f"answer_{key}": sum(request.get(key, 0) for request in requests) for key in keys}
    row["execution_receipt"] = json.dumps({
        "request_usage_version": 1,
        "coverage": coverage,
        "response_count": len(requests),
        "request_usage": [
            {"response_id": f"r-{index}", "usage": request}
            for index, request in enumerate(requests)
        ],
    })
    return row


def request_usage(inputs, cached=0, writes=0, output=0):
    return dict(zip(
        ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens"),
        (inputs, cached, writes, output), strict=True,
    ))


def test_multiple_short_requests_do_not_receive_long_context_rates():
    row = request_cost_row([request_usage(200_000), request_usage(200_000)])
    assert turn_cost(row, "gpt-6.1-terra", default_config().pricing) == pytest.approx(0.40)


def test_mixed_request_tiers_include_cache_writes_and_separate_output_multiplier():
    requests = [request_usage(200_000), request_usage(300_000, 100_000, 10_000, 10_000)]
    row = request_cost_row(requests)
    # $0.20 short request + $0.38 fresh + $0.01 read + $0.025 write + $0.075 output.
    assert turn_cost(row, "gpt-6.1-terra", default_config().pricing) == pytest.approx(0.69)


@pytest.mark.parametrize("coverage", ["partial", "turn_start_seen"])
def test_partial_or_mismatched_request_usage_is_excluded(coverage):
    row = request_cost_row([request_usage(300_000)], coverage=coverage)
    if coverage == "turn_start_seen":
        row["answer_input_tokens"] += 1
    assert turn_cost(row, "gpt-6.1-terra", default_config().pricing) is None


@pytest.mark.parametrize("kind", ["missing_cache", "duplicate", "invalid", "wrong_count"])
def test_invalid_request_receipts_do_not_invent_prices(kind):
    row = request_cost_row([request_usage(200_000), request_usage(200_000)])
    receipt = json.loads(row["execution_receipt"])
    if kind == "missing_cache":
        del receipt["request_usage"][0]["usage"]["cache_write_input_tokens"]
    elif kind == "duplicate":
        receipt["request_usage"][1]["response_id"] = "r-0"
    elif kind == "invalid":
        receipt["request_usage"][0]["usage"]["cached_input_tokens"] = 200_001
    else:
        receipt["response_count"] = 3
    row["execution_receipt"] = json.dumps(receipt)
    assert turn_cost(row, "gpt-6.1-terra", default_config().pricing) is None


def test_legacy_totals_above_threshold_cannot_prove_request_price_tier():
    pricing = default_config().pricing
    row = request_cost_row([request_usage(200_000), request_usage(200_000)])
    row["execution_receipt"] = "{}"
    assert turn_cost(row, "gpt-6.1-terra", pricing) is None
    assert turn_cost(row, "gpt-6-astra", pricing) == pytest.approx(4.0)
    row["answer_input_tokens"] = 272_000
    assert turn_cost(row, "gpt-6.1-terra", pricing) == pytest.approx(0.272)


def test_legacy_unknown_cache_split_remains_unpriced():
    row = request_cost_row([request_usage(200_000)])
    row["execution_receipt"] = "{}"
    row["answer_cached_input_tokens"] = None
    row["answer_cache_write_input_tokens"] = None
    assert turn_cost(row, "gpt-6.1-terra", default_config().pricing) is None


def test_unknown_request_cache_split_has_a_conservative_budget_bound():
    pricing = default_config().pricing
    partial = request_usage(300_000, 200_000, 0, 10)
    bound = token_cost_upper_bound("gpt-6.1-terra", partial, pricing)
    actual = token_cost("gpt-6.1-terra", request_usage(300_000, 200_000, 90_000, 10), pricing)
    assert actual == pytest.approx(0.265075)
    assert bound == pytest.approx(0.750075)
    assert bound >= actual


def test_cost_report_compares_only_turns_with_complete_request_prices(tmp_path):
    config = default_config()
    store = AuditStore(tmp_path / "audit.db")
    for turn in ("requests", "legacy"):
        context = RouteContext(session_id="s", turn_id=turn, latest_prompt="x")
        decision = Router(config).route(context)
        decision.model = "gpt-6.1-terra"
        decision.classifier_usage = {"prompt_tokens": 100, "completion_tokens": 10}
        decision.selection_receipt["classifier"]["model"] = "gpt-5.6-luna"
        store.record(decision, Tier.NORMAL)
        store.record_usage("s", turn, "gpt-6.1-terra", request_usage(400_000))
        if turn == "requests":
            fixture = request_cost_row([request_usage(200_000), request_usage(200_000)])
            receipt = json.loads(fixture["execution_receipt"])
            store.record_execution("s", turn, receipt, "root", None)
    report = cost_report(store.history(limit=10), config.pricing, "gpt-6-astra")
    assert report.measured_turns == 2
    assert report.priced_turns == 1
    assert report.incomplete_pricing_turns == 1
    assert report.input_tokens == 800_000
    assert report.actual_cost == pytest.approx(0.4)
    assert report.baseline_cost == pytest.approx(4.0)
    assert report.classifier_cost == pytest.approx(2 * report.comparison_classifier_cost)
    assert report.net_savings == pytest.approx(3.6 - report.comparison_classifier_cost)


def test_token_cost_separates_cached_and_uncached_input():
    pricing = default_config().pricing
    cost = token_cost(
        "gpt-5.6-luna",
        {"input_tokens": 1_000_000, "cached_input_tokens": 800_000, "output_tokens": 100_000},
        pricing,
    )
    assert cost == pytest.approx(0.176)


@pytest.mark.parametrize(
    ("input_tokens", "expected"),
    [(271_999, 0.229499), (272_000, 0.2295), (272_001, 0.434002)],
)
def test_terra_prices_each_request_at_the_exact_long_context_boundary(input_tokens, expected):
    # 100k cached, 10k written, 10k output; above 272k, input doubles but output is 1.5x.
    usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": 100_000,
        "cache_write_input_tokens": 10_000,
        "output_tokens": 10_000,
    }
    assert token_cost("gpt-6.1-terra", usage, default_config().pricing) == pytest.approx(expected)


def test_terra_default_price_does_not_override_a_saved_custom_rate():
    config = default_config()
    config.pricing.models["gpt-6.1-terra"].input_per_million = 1.10
    assert _with_default_backends(config).pricing.models["gpt-6.1-terra"].input_per_million == 1.10


@pytest.mark.parametrize("inclusive", [False, True])
def test_long_context_legacy_output_multiplier_and_boundary(inclusive):
    pricing = default_config().pricing
    rate = pricing.models["grok-4.7"]
    rate.long_context_threshold_inclusive = inclusive
    usage = {"input_tokens": 200_000, "output_tokens": 10_000}
    expected = 0.92 if inclusive else 0.46
    assert token_cost("grok-4.7", usage, pricing) == pytest.approx(expected)


def test_provider_prefixed_model_uses_public_model_price():
    pricing = default_config().pricing
    cost = token_cost(
        "provider/gpt-5.6-luna",
        {"input_tokens": 1_000_000, "output_tokens": 0},
        pricing,
    )
    assert cost == pytest.approx(0.20)


def test_gpt_6_1_sol_default_has_published_price_and_capabilities():
    config = default_config()
    assert config.backends["gpt"].tiers["smart"].model == "gpt-6.1-sol"
    assert config.backends["gpt"].tiers["smart"].reasoning_effort == "high"
    price = config.pricing.models["gpt-6.1-sol"]
    assert (
        price.input_per_million,
        price.cached_input_per_million,
        price.cache_write_per_million,
        price.output_per_million,
    ) == (2.0, 0.10, 2.50, 10.0)
    assert config.capabilities.models["gpt-6.1-sol"].context_window == 872_000


def test_old_bundled_gpt_6_1_context_migrates_without_overriding_custom_limit():
    config = default_config()
    config.capabilities.models["gpt-6.1-sol"] = ModelCapabilities(
        tool_calling="full",
        reasoning=True,
        vision=True,
        context_window=1_050_000,
        pricing_model="gpt-6.1-sol",
    )
    assert (
        _with_default_backends(config).capabilities.models["gpt-6.1-sol"].context_window
        == 872_000
    )

    config = default_config()
    config.capabilities.models["gpt-6.1-sol"].context_window = 900_000
    assert (
        _with_default_backends(config).capabilities.models["gpt-6.1-sol"].context_window
        == 900_000
    )


def test_existing_gpt_smart_default_migrates_without_overriding_custom_target():
    config = default_config()
    config.backends["gpt"].tiers["smart"] = ModelTarget(
        model="gpt-6-sol", reasoning_effort="high"
    )
    assert _with_default_backends(config).backends["gpt"].tiers["smart"].model == "gpt-6.1-sol"

    config.backends["gpt"].tiers["smart"] = ModelTarget(
        model="gpt-6-sol", reasoning_effort="xhigh"
    )
    assert _with_default_backends(config).backends["gpt"].tiers["smart"].model == "gpt-6-sol"


@pytest.mark.parametrize(
    ("model", "input_rate", "cache_write_rate", "cached_rate", "output_rate"),
    [
        # Anthropic's published rates: platform.claude.com/docs/en/about-claude/pricing
        ("claude-haiku-4-5-20251001", 1.0, 1.25, 0.10, 5.0),
        ("claude-sonnet-5", 2.0, 2.50, 0.20, 10.0),
        ("claude-sonnet-5-5", 2.0, 2.50, 0.20, 10.0),
        ("claude-fable-5-1", 10.0, 12.50, 0.25, 50.0),
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
    assert report.actual_cost == 0
    assert report.baseline_cost == 0
    assert report.priced_turns == 0
    assert report.gross_savings is None
    assert report.net_savings is None
    assert report.savings_percent is None


def test_savings_uses_classifier_overhead_from_same_priced_turns(tmp_path):
    config = default_config()
    store = AuditStore(tmp_path / "audit.db")
    for turn, model in [("priced", "gpt-5.6-luna"), ("unknown", "unpriced")]:
        decision = Router(config).route(
            RouteContext(session_id="s", turn_id=turn, latest_prompt="x")
        )
        decision.model = model
        decision.classifier_usage = {"prompt_tokens": 100, "completion_tokens": 10}
        decision.selection_receipt["classifier"]["model"] = "gpt-5.6-luna"
        store.record(decision, Tier.NORMAL)
        store.record_usage("s", turn, model, {"input_tokens": 100, "output_tokens": 10})
    report = cost_report(store.history(limit=10), config.pricing, "gpt-6-astra")
    assert report.priced_turns == 1
    assert report.classifier_cost == pytest.approx(2 * report.comparison_classifier_cost)
    assert report.net_savings == pytest.approx(
        report.baseline_cost - report.actual_cost - report.comparison_classifier_cost
    )


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
