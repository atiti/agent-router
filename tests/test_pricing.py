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
