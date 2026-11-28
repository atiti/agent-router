"""YOLO routing: provider priority, pins, effort translation and hook continuation."""

import json

import pytest
from test_hook import invoke

from agentroute.audit import AuditStore
from agentroute.claude_models import claude_effort
from agentroute.config import ModelTarget, default_config
from agentroute.grok_bridge import ensure_backend as ensure_grok_backend
from agentroute.models import ReasonCode, RouteContext, Tier
from agentroute.providers import ensure_claude_bridge_backend
from agentroute.router import Router


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    # Never probe a real local bridge from tests.
    monkeypatch.setattr("agentroute.provider_selection.local_bridge_ready", lambda *_: True)
    config = default_config()
    config.policy.max_tier = "yolo"
    ensure_claude_bridge_backend(config, 8090)
    ensure_grok_backend(config)
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    config.backends["deepseek"].enabled = True
    return config


def route(config, prompt, **kwargs):
    return Router(config).route(RouteContext(session_id="yolo", latest_prompt=prompt, **kwargs))


# --- Effort translation --------------------------------------------------------


@pytest.mark.parametrize(
    ("prompt", "backend", "model", "effort"),
    [
        ("@gpt @yolo review this design", "gpt", "gpt-6.2-astra", "ultra"),
        ("@azure @yolo use my Astra deployment", "azure", "gpt-6.2-astra", "ultra"),
        ("@claude @yolo investigate the failure", "claude", "claude-fable-5-2", "ultra"),
        ("@fable @yolo investigate the failure", "fable", "claude-fable-5-2", "ultra"),
        ("@grok @yolo research this question", "grok", "grok-4.7", "xhigh"),
    ],
)
def test_explicit_yolo_routes_keep_provider_effort(config, prompt, backend, model, effort):
    decision = route(config, prompt)
    assert not decision.capacity_blocked, decision.capacity_detail
    assert (decision.tier, decision.backend, decision.model, decision.reasoning_effort) == (
        Tier.YOLO, backend, model, effort
    )
    selected = decision.selection_receipt["selected"]
    assert (selected["tier"], selected["model"], selected["reasoning_effort"]) == (
        "yolo", model, effort
    )
    assert decision.selection_receipt["provider_selection"]["locked_by"] == "explicit_backend"


def test_claude_ultra_becomes_provider_max():
    assert claude_effort("claude-fable-5-2", "ultra") == "max"


def test_grok_yolo_equals_max(config):
    grok = config.backends["grok"]
    assert grok.tiers["yolo"] == grok.tiers["max"]


@pytest.mark.parametrize(
    ("backend", "target", "reason"),
    [
        ("grok", ModelTarget(model="grok-4.7", reasoning_effort="ultra"), "unsupported_effort"),
        ("gpt", ModelTarget(model="gpt-6.2-astra", reasoning_effort="max"), "unsupported_effort"),
        (
            "claude",
            ModelTarget(model="claude-haiku-4-5-20251001", reasoning_effort="ultra"),
            "unsupported_effort",
        ),
    ],
)
def test_unsupported_effort_is_visibly_unavailable(config, backend, target, reason):
    config.backends[backend].tiers["yolo"] = target
    decision = route(config, f"@{backend} @yolo go")
    assert decision.capacity_blocked
    assert decision.capacity_trigger == "yolo_unavailable"
    assert reason in (decision.capacity_detail or "")
    assert decision.selection_receipt["yolo"]["status"] == "blocked"
    # Never a silently lowered effort or a MAX route under the YOLO label.
    assert decision.reasoning_effort != "xhigh" or backend == "grok"


def test_missing_mapping_is_unavailable_not_max(config):
    decision = route(config, "@deepseek @yolo go")
    assert decision.capacity_blocked
    assert decision.backend == "deepseek"
    assert "no_yolo_mapping" in (decision.capacity_detail or "")
    assert decision.model != config.backends["deepseek"].tiers["max"].model


# --- Provider priority ----------------------------------------------------------


def test_subscription_beats_azure_despite_scores(config):
    config.backends["gpt"].enabled = False
    decision = route(
        config,
        "@yolo solve this difficult problem",
        current_model="gpt-5",
        current_model_provider="agentroute-azure",
        previous_response_usage={"input_tokens": 200_000},
    )
    assert not decision.capacity_blocked, decision.capacity_detail
    assert decision.backend in {"claude", "grok"}
    receipt = decision.selection_receipt["provider_selection"]
    rows = {row["backend"]: row for row in receipt["candidates"]}
    assert rows["azure"]["eligible"]
    # Azure scores higher (no provider switch), yet the subscription group wins.
    assert rows["azure"]["score"] > rows[decision.backend]["score"]
    assert receipt["selection_pool"] == "yolo_subscription"
    assert receipt["yolo_priority"][-1] == "azure"
    assert rows["deepseek"]["exclusions"] == ["no_yolo_mapping"]


def test_azure_is_reached_only_after_subscriptions_are_ineligible(config, monkeypatch):
    for name in ("gpt", "claude", "grok"):
        config.backends[name].enabled = False
    decision = route(config, "@yolo solve this difficult problem")
    assert not decision.capacity_blocked, decision.capacity_detail
    assert (decision.backend, decision.model, decision.reasoning_effort) == (
        "azure", "gpt-6.2-astra", "ultra"
    )
    assert decision.selection_receipt["provider_selection"]["selection_pool"] == "yolo_azure"


def test_no_eligible_yolo_route_is_explicit(config):
    for name in ("gpt", "claude", "grok", "azure"):
        config.backends[name].enabled = False
    decision = route(config, "@yolo solve this")
    assert decision.capacity_blocked
    assert decision.capacity_trigger == "yolo_unavailable"
    assert "no eligible provider" in (decision.capacity_detail or "")


def test_static_routing_without_yolo_is_unchanged(config):
    decision = route(config, "@max design the system")
    assert decision.backend == "gpt"
    assert decision.selection_receipt["provider_selection"]["candidates"] == []


# --- Pins -------------------------------------------------------------------------


def test_pinned_unavailable_backend_is_not_substituted(config, monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_API_KEY")
    decision = route(config, "@azure @yolo use my Astra deployment")
    assert decision.capacity_blocked
    assert decision.backend == "azure"
    assert "pinned backend azure is unavailable" in (decision.capacity_detail or "")


def test_session_pin_is_binding_for_yolo_continuation(config):
    decision = route(
        config,
        "ok continue",
        sticky_backend="claude",
        current_tier=Tier.YOLO,
        previous_task_tier=Tier.YOLO,
        previous_yolo_opt_in="explicit_tag",
    )
    assert (decision.tier, decision.backend, decision.model) == (
        Tier.YOLO, "claude", "claude-fable-5-2"
    )


def test_account_yolo_override_and_unsupported_account_target(config):
    config.capacity.profiles["default"].tiers["yolo"] = ModelTarget(
        model="gpt-6-astra", reasoning_effort="ultra"
    )
    decision = route(config, "@gpt @yolo go")
    assert decision.model == "gpt-6-astra"
    config.capacity.profiles["default"].tiers["yolo"].reasoning_effort = "max"
    decision = route(config, "@gpt @yolo go")
    assert decision.capacity_blocked
    assert "unsupported_effort:gpt-6-astra/max" in (decision.capacity_detail or "")


# --- Hook: visible admission block and recorded continuation -----------------------


def test_hook_blocks_unadmitted_yolo_visibly(tmp_path):
    config = default_config()
    config.enabled = True
    output = invoke(config, AuditStore(tmp_path / "audit.db"), "@yolo solve this")
    assert output["continue"] is False
    assert "policy.max_tier" in output["systemMessage"]
    assert "policy.max_tier" in output["stopReason"]


def test_hook_continuation_keeps_yolo_and_new_task_exits(tmp_path):
    config = default_config()
    config.enabled = True
    config.policy.max_tier = "yolo"
    store = AuditStore(tmp_path / "audit.db")
    first = invoke(config, store, "@yolo design the replication protocol", turn_id="t1")
    assert first["hookSpecificOutput"]["model"] == "gpt-6.2-astra"
    assert first["hookSpecificOutput"]["reasoningEffort"] == "ultra"
    row = store.latest_route("same-thread", "root", None)
    assert row["selected_tier"] == "yolo"
    assert json.loads(row["selection_receipt"])["yolo"]["opt_in_source"] == "explicit_tag"

    second = invoke(config, store, "ok continue", model="gpt-6.2-astra", turn_id="t2")
    assert second["hookSpecificOutput"]["reasoningEffort"] == "ultra"
    row = store.latest_route("same-thread", "root", None)
    assert row["selected_tier"] == "yolo"
    receipt = json.loads(row["selection_receipt"])
    assert receipt["yolo"]["opt_in_source"] == "continuation"
    assert ReasonCode.YOLO_OPT_IN.value in row["reason_codes"]

    invoke(config, store, "@max now tidy the README", model="gpt-6.2-astra", turn_id="t3")
    assert store.latest_route("same-thread", "root", None)["selected_tier"] == "max"
    invoke(config, store, "ok continue", model="gpt-6.2-astra", turn_id="t4")
    assert store.latest_route("same-thread", "root", None)["selected_tier"] != "yolo"


# --- Blocked attempts never leave the thread in YOLO --------------------------------


def test_blocked_yolo_then_continue_routes_normally(tmp_path):
    config = default_config()
    config.enabled = True
    store = AuditStore(tmp_path / "audit.db")
    blocked = invoke(config, store, "@yolo do X", turn_id="t1")
    assert blocked["continue"] is False
    assert "policy.max_tier" in blocked["systemMessage"]
    row = store.latest_route("same-thread", "root", None)
    assert row["capacity_status"] == "yolo_blocked"
    receipt = json.loads(row["selection_receipt"])["yolo"]
    assert receipt["status"] == "blocked"
    assert receipt["opt_in_source"] is None
    assert receipt["attempted_opt_in"] == "explicit_tag"

    followup = invoke(config, store, "continue", turn_id="t2")
    assert followup["continue"] is True
    row = store.latest_route("same-thread", "root", None)
    assert row["selected_tier"] in {"fast", "normal", "smart", "max"}
    assert not row["capacity_blocked"]
    assert "yolo" not in json.loads(row["selection_receipt"])

    again = invoke(config, store, "@yolo do X", turn_id="t3")
    assert again["continue"] is False


def test_unavailable_pinned_yolo_then_continue_routes_normally(tmp_path, monkeypatch):
    config = default_config()
    config.enabled = True
    config.policy.max_tier = "yolo"
    config.backends["deepseek"].enabled = True
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    store = AuditStore(tmp_path / "audit.db")
    assert invoke(config, store, "@deepseek @yolo do X", turn_id="t1")["continue"] is False
    followup = invoke(config, store, "continue", turn_id="t2")
    assert followup["continue"] is True
    assert store.latest_route("same-thread", "root", None)["selected_tier"] != "yolo"


def test_successful_yolo_then_continue_keeps_yolo(tmp_path):
    config = default_config()
    config.enabled = True
    config.policy.max_tier = "yolo"
    store = AuditStore(tmp_path / "audit.db")
    assert invoke(config, store, "@yolo do X", turn_id="t1")["continue"] is True
    invoke(config, store, "continue", model="gpt-6.2-astra", turn_id="t2")
    row = store.latest_route("same-thread", "root", None)
    assert row["selected_tier"] == "yolo"
    assert json.loads(row["selection_receipt"])["yolo"]["opt_in_source"] == "continuation"
