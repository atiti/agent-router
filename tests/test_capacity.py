import os
import time
from datetime import datetime

import pytest

from agentroute.capacity import fallback_chain, local_time_description, subscription_state
from agentroute.config import default_config
from agentroute.models import ReasonCode, RouteContext
from agentroute.router import Router


def enabled_config():
    config = default_config()
    config.capacity.enabled = True
    return config


def test_authoritative_false_overrides_sparse_quota_data():
    state = subscription_state(
        enabled_config(),
        {"ordinaryUsageAllowed": False, "rateLimits": {"primary": {"usedPercent": 2}}},
        "private-account",
    )

    assert state.status == "exhausted"
    assert state.trigger == "subscription"


def test_warning_and_recovery_hysteresis_are_explicit():
    config = enabled_config()
    warning = subscription_state(config, {"primary": {"usedPercent": 90}})
    held = subscription_state(
        config, {"primary": {"usedPercent": 80}}, recovery_hold=True
    )

    assert warning.status == "warning"
    assert warning.detail == "subscription 90% used / 10% remaining"
    assert held.status == "exhausted"
    assert held.trigger == "subscription_recovery"


def test_subscription_usage_reports_used_and_remaining_percentages():
    state = subscription_state(enabled_config(), {"primary": {"usedPercent": 62}})

    assert state.status == "healthy"
    assert state.detail == "subscription 62% used / 38% remaining"


def test_reset_description_uses_the_local_timezone(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("changing the process timezone requires time.tzset")
    prior_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        expected = datetime.fromisoformat("2026-09-26T22:00:00+00:00")
        assert local_time_description("2026-09-26T22:00:00Z") == expected.astimezone().strftime(
            "%Y-%m-%d %H:%M %Z"
        )
    finally:
        if prior_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", prior_tz)
        time.tzset()


def test_fallback_ring_is_bounded_and_deduplicated():
    config = enabled_config()

    assert fallback_chain(config, "gpt") == ["azure", "deepseek"]


def test_budget_exhaustion_uses_next_ready_backend(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    config = enabled_config()
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    config.backends["gpt"].daily_budget_usd = 1  # ignored: subscription is authoritative
    config.backends["azure"].daily_budget_usd = 1

    decision = Router(config).route(
        RouteContext(
            session_id="capacity-test",
            latest_prompt="check status",
            backend_daily_spend={"azure": 1},
        )
    )

    # GPT has unknown subscription telemetry so it remains valid; make Azure explicit to
    # demonstrate budget failover is guarded rather than silently selecting an over-budget API.
    explicit = Router(config).route(
        RouteContext(
            session_id="capacity-test-2",
            latest_prompt="@azure check status",
            backend_daily_spend={"azure": 1},
        )
    )
    assert decision.capacity_blocked is False
    assert explicit.capacity_blocked is True
    assert ReasonCode.CAPACITY_BLOCKED in explicit.reason_codes


def test_automatic_subscription_failover_strips_provider_state(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    config = enabled_config()
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"

    decision = Router(config).route(
        RouteContext(
            session_id="capacity-test",
            latest_prompt="check status",
            current_model_provider="openai",
            rate_limits={"ordinary_usage_allowed": False},
        )
    )

    assert decision.backend == "azure"
    assert decision.capacity_status == "fallback"
    assert ReasonCode.CAPACITY_FALLBACK in decision.reason_codes


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("limit_id", ["claude", "deepseek"])
def test_foreign_limits_do_not_exhaust_chatgpt_capacity(nested, limit_id):
    limits = {
        "limit_id": limit_id,
        "primary": {"used_percent": 100},
        "spend_control_reached": True,
        "rate_limit_reached_type": "rate_limit_reached",
    }
    snapshot = {"rateLimits": limits} if nested else limits
    state = subscription_state(enabled_config(), snapshot)

    assert state.status == "unknown"
    assert state.available
    assert state.used_percent is None


def test_foreign_limits_preserve_independent_chatgpt_account_lock():
    state = subscription_state(enabled_config(), {
        "ordinaryUsageAllowed": False,
        "rateLimits": {"limitId": "claude", "primary": {"usedPercent": 100}},
    })

    assert state.status == "exhausted"
    assert state.used_percent is None


@pytest.mark.parametrize("limit_id", [None, "codex", "codex_other", "codex-other"])
def test_chatgpt_limit_families_still_enforce_exhaustion(limit_id):
    state = subscription_state(enabled_config(), {
        "limit_id": limit_id,
        "primary": {"usedPercent": 100},
    })

    assert state.status == "exhausted"
    assert state.used_percent == 100
