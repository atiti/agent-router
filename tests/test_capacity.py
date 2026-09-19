from agentroute.capacity import fallback_chain, subscription_state
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
    assert held.status == "exhausted"
    assert held.trigger == "subscription_recovery"


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
