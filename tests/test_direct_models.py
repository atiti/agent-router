import io
import itertools
import json

import pytest

from agentroute.audit import AuditStore
from agentroute.config import ModelTarget, SubscriptionProfileConfig, default_config
from agentroute.hook import codex_user_prompt_submit
from agentroute.model_selection import DirectModelError, resolve_model
from agentroute.models import ReasonCode, RouteContext, Tier
from agentroute.router import Router
from agentroute.signals import model_route_overrides


def configured(monkeypatch):
    config = default_config()
    config.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    config.backends["azure"].tiers["normal"] = ModelTarget(
        model="gpt-6.1-sol", reasoning_effort="high"
    )
    config.backends["azure"].models["gpt-6.1-terra"] = ModelTarget(
        model="gpt-6.1-terra", reasoning_effort="medium", context_window=922000
    )
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    return config


@pytest.mark.parametrize("tags", list(itertools.permutations(
    ("@azure", "@terra", "@normal", "@xhigh")
)))
def test_four_prefix_categories_parse_in_any_order(tags):
    result = model_route_overrides(" ".join(tags) + " Árvíztűrő task", {"azure"})
    assert result == ("normal", "azure", "xhigh", "terra", None, "Árvíztűrő task")


def test_exact_id_case_is_preserved_and_body_mentions_are_not_consumed():
    assert model_route_overrides("@model:Custom-ID @azure task with @sol", {"azure"}) == (
        None, "azure", None, "Custom-ID", None, "task with @sol"
    )
    assert model_route_overrides("task @terra", {"azure"})[-1] == "task @terra"


def test_profile_prefix_in_any_position_and_existing_custom_source():
    result = model_route_overrides("@sonnet @xhigh @claude:team @normal hello", {"claude"})
    assert result == ("normal", "claude", "xhigh", "sonnet", "team", "hello")
    assert model_route_overrides("@qwen @normal hello", {"qwen"})[1] == "qwen"
    assert model_route_overrides("@sol @normal hello", {"sol"})[1] == "sol"


@pytest.mark.parametrize("prompt", ["@terra @sol hello", "@model: hello",
                                    "@auto @terra hello", "@azure @gpt @terra hello"])
def test_conflicting_or_malformed_explicit_selection_rejected(prompt):
    with pytest.raises(DirectModelError):
        model_route_overrides(prompt, {"azure", "gpt"})


def test_direct_terra_bypasses_classifier_and_interrupt_affinity(monkeypatch):
    config = configured(monkeypatch)

    class NeverClassify:
        def classify(self, context):
            raise AssertionError("Explicit model must not call classifier")

    config.routing.classifier.enabled = True
    decision = Router(config, classifier=NeverClassify()).route(RouteContext(
        session_id="direct", latest_prompt="@azure @terra @xhigh handle this",
        current_model="gpt-6.1-sol", current_model_provider="agentroute-azure",
        previous_task_tier=Tier.NORMAL, interrupted_turn_affinity=True,
        interrupted_backend="azure", interrupted_model="gpt-6.1-sol",
    ))
    assert decision.model == "gpt-6.1-terra"
    assert decision.reasoning_effort == "xhigh"
    assert decision.manual_override and decision.switched
    assert ReasonCode.MODEL_OVERRIDE in decision.reason_codes
    assert decision.selection_receipt["explicit_model"]["requested"] == "terra"


def invoke(config, store, prompt, model="gpt-6.1-sol", provider="agentroute-azure", subagent=None):
    source = io.StringIO(json.dumps({"session_id": "direct-session", "turn_id": prompt,
                                    "model": model, "model_provider": provider,
                                    "prompt": prompt, "subagent": subagent}))
    sink = io.StringIO()
    codex_user_prompt_submit(source, sink, config=config, store=store)
    return json.loads(sink.getvalue())


def test_hook_enforces_model_and_strips_unicode_prefix(monkeypatch, tmp_path):
    config = configured(monkeypatch)
    output = invoke(config, AuditStore(tmp_path / "audit.db"),
                    "@azure @terra @xhigh Árvíz")
    assert output["continue"]
    assert output["hookSpecificOutput"]["model"] == "gpt-6.1-terra"
    assert output["hookSpecificOutput"]["reasoningEffort"] == "xhigh"
    assert output["hookSpecificOutput"]["stripPromptPrefixBytes"] == len(
        b"@azure @terra @xhigh "
    )


def test_unavailable_source_or_unknown_model_blocks_without_fallback(monkeypatch, tmp_path):
    config = configured(monkeypatch)
    config.capacity.enabled = False
    unknown = invoke(config, AuditStore(tmp_path / "unknown.db"), "@gpt @model:Azure-Only hello")
    assert unknown["continue"] is False
    config.backends["azure"].enabled = False
    disabled = invoke(config, AuditStore(tmp_path / "disabled.db"), "@azure @terra hello")
    assert disabled["continue"] is False
    assert "unavailable" in disabled["stopReason"]


def test_ambiguous_model_family_requires_exact_id(monkeypatch):
    config = configured(monkeypatch)
    config.backends["azure"].tiers["smart"] = ModelTarget(model="gpt-6-sol")
    with pytest.raises(DirectModelError, match="ambiguous"):
        resolve_model(config, "azure", "sol")


def test_selected_gpt_profile_cannot_use_another_profiles_declared_model(monkeypatch):
    config = configured(monkeypatch)
    config.capacity.profiles = {
        "one": SubscriptionProfileConfig(models={"account-only": ModelTarget(
            model="account-only", context_window=272000
        )}),
        "two": SubscriptionProfileConfig(),
    }
    assert resolve_model(config, "gpt", "account-only", "one").model == "account-only"
    with pytest.raises(DirectModelError):
        resolve_model(config, "gpt", "account-only", "two")


def test_explicit_model_survives_continuation_but_tier_clears_it(monkeypatch, tmp_path):
    config = configured(monkeypatch)
    store = AuditStore(tmp_path / "audit.db")
    invoke(config, store, "@azure @terra @xhigh hello")
    continued = invoke(config, store, "continue", model="gpt-6.1-terra")
    assert continued["hookSpecificOutput"]["model"] == "gpt-6.1-terra"
    normal = invoke(config, store, "@azure @normal hello", model="gpt-6.1-terra")
    assert normal["hookSpecificOutput"]["model"] == "gpt-6.1-sol"
    assert continued["hookSpecificOutput"]["reasoningEffort"] == "xhigh"


def test_explicit_source_switch_strips_foreign_provider_state(monkeypatch, tmp_path):
    config = configured(monkeypatch)
    output = invoke(config, AuditStore(tmp_path / "audit.db"), "@gpt @luna @xhigh hello")
    assert output["hookSpecificOutput"]["modelProvider"] == "openai"
    assert output["hookSpecificOutput"]["stripProviderState"] is True


def test_additional_declared_model_requires_source_window(monkeypatch):
    config = configured(monkeypatch)
    config.backends["azure"].models["custom"] = ModelTarget(model="custom")
    with pytest.raises(DirectModelError, match="context_window"):
        resolve_model(config, "azure", "custom")


def test_declared_model_window_is_emitted_in_provider_capabilities(monkeypatch):
    from agentroute.providers import _provider_context_windows

    config = configured(monkeypatch)
    monkeypatch.setenv("AGENTROUTE_RUNTIME_BUILD_ID", "abc-provider-routing-v61")
    assert _provider_context_windows(config, "azure")["gpt-6.1-terra"] == 922000


def test_rejected_explicit_selection_records_reason(monkeypatch, tmp_path):
    config = configured(monkeypatch)
    store = AuditStore(tmp_path / "audit.db")
    output = invoke(config, store, "@azure @model:unknown hello")
    assert output["continue"] is False
    receipt = json.loads(store.latest("direct-session")["selection_receipt"])
    assert receipt["explicit_model"]["requested"] == "unknown"
    assert "not declared" in receipt["explicit_model"]["rejection"]


def test_explicit_model_respects_policy_cap(monkeypatch):
    config = configured(monkeypatch)
    config.policy.max_tier = "normal"
    with pytest.raises(DirectModelError, match="max_tier"):
        Router(config).route(RouteContext(
            session_id="cap", latest_prompt="@gpt @astra hello"
        ))
    with pytest.raises(DirectModelError, match="max_tier"):
        Router(config).route(RouteContext(
            session_id="masked-cap", latest_prompt="@gpt @normal @astra hello"
        ))
    config.capacity.active_profile = "team"
    config.capacity.profiles["team"] = SubscriptionProfileConfig(
        tiers={"max": ModelTarget(model="gpt-5.6-sol")}
    )
    with pytest.raises(DirectModelError, match="max_tier"):
        Router(config).route(RouteContext(
            session_id="profile-cap", latest_prompt="@gpt @normal @model:gpt-5.6-sol hello"
        ))


def test_no_account_selection_result_preserves_valid_gpt_direct_model(monkeypatch, tmp_path):
    from unittest.mock import patch

    config = configured(monkeypatch)
    with patch("agentroute.hook.select_turn_profile", return_value=None):
        result = invoke(config, AuditStore(tmp_path / "audit.db"), "@gpt @luna @xhigh hello")
    assert result["continue"]
    assert result["hookSpecificOutput"]["model"] == "gpt-6-luna"


def test_explicit_model_uses_inherited_child_source(monkeypatch):
    config = configured(monkeypatch)
    config.routing.backend_by_tier["normal"] = "azure"
    decision = Router(config).route(RouteContext(
        session_id="child", latest_prompt="@sol @xhigh hello", inherited_backend="gpt"
    ))
    assert decision.backend == "gpt"


def test_direct_same_model_preserves_selected_tier_effort(monkeypatch):
    config = configured(monkeypatch)
    config.routing.reasoning_effort_policy = "tier_default"
    config.backends["azure"].tiers["fast"] = ModelTarget(
        model="gpt-6.1-terra", reasoning_effort="medium"
    )
    config.backends["azure"].tiers["normal"] = ModelTarget(
        model="gpt-6.1-terra", reasoning_effort="xhigh"
    )
    decision = Router(config).route(RouteContext(
        session_id="fast", latest_prompt="@azure @fast @terra hello"
    ))
    assert decision.reasoning_effort == "medium"


def test_explicit_request_cannot_fail_open_on_audit_error(monkeypatch):
    import sqlite3
    from unittest.mock import Mock

    config = configured(monkeypatch)
    store = Mock()
    store.latest_route.side_effect = sqlite3.OperationalError("database is locked")
    output = invoke(config, store, "@gpt @sol @xhigh continue")
    assert output["continue"] is False


def test_child_rejection_does_not_pollute_parent_history(monkeypatch, tmp_path):
    config = configured(monkeypatch)
    store = AuditStore(tmp_path / "audit.db")
    output = invoke(config, store, "@azure @model:missing hello",
                    subagent={"agent_id": "child", "agent_type": "worker"})
    assert output["continue"] is False
    assert store.latest_route("direct-session", "root", None) is None
    row = store.latest_route("direct-session", "subagent", "child")
    assert row["backend"] == "azure"
    bad = invoke(config, store, "@terra @sol hello",
                 subagent={"agent_id": "parse-child", "agent_type": "worker"})
    assert bad["continue"] is False
    assert store.latest_route("direct-session", "root", None) is None
    assert store.latest_route("direct-session", "subagent", "parse-child") is not None


def test_account_selection_precedes_direct_account_model_admission(monkeypatch, tmp_path):
    from unittest.mock import patch

    from agentroute.capacity import CapacityState
    from agentroute.profiles import ProfileStatus, TurnProfileSelection

    config = configured(monkeypatch)
    config.capacity.active_profile = None
    config.capacity.profiles["selected"] = SubscriptionProfileConfig(models={
        "profile-only": ModelTarget(model="profile-only", context_window=272000)
    })
    status = ProfileStatus("selected", str(tmp_path), 0, True, True, "hash",
                           CapacityState("gpt", "healthy", "healthy"))
    selected = TurnProfileSelection("selected", status, status, False, "account_match", False)
    with patch("agentroute.hook.select_turn_profile", return_value=selected):
        result = invoke(config, AuditStore(tmp_path / "audit.db"),
                        "@gpt @model:profile-only @xhigh hello")
    assert result["continue"]
    assert result["hookSpecificOutput"]["model"] == "profile-only"


def test_rejected_model_is_not_inherited_on_continuation(monkeypatch, tmp_path):
    config = configured(monkeypatch)
    store = AuditStore(tmp_path / "audit.db")
    assert invoke(config, store, "@azure @model:missing hello")["continue"] is False
    result = invoke(config, store, "continue")
    assert result["hookSpecificOutput"]["model"] != "missing"
