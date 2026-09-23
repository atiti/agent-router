import io
import json
from unittest.mock import patch

from agentroute.audit import AuditStore
from agentroute.capacity import CapacityState
from agentroute.config import (
    ExecutionBackendConfig,
    ModelTarget,
    SubscriptionProfileConfig,
    default_config,
)
from agentroute.hook import codex_stop, codex_user_prompt_submit
from agentroute.profiles import ProfileStatus, reviewer_fallback_profiles


class FakeClassifierResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        content = json.dumps(
            {
                "tier": "SMART",
                "reasoning_effort": "HIGH",
                "confidence": 0.87,
                "task_type": "implementation",
                "reason": "The task requires substantial implementation judgment.",
            }
        )
        return json.dumps(
            {
                "model": "classifier-fast",
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
                "choices": [{"message": {"content": content}}],
            }
        ).encode()


def invoke(
    config,
    store,
    prompt,
    model="gpt-5.6-terra",
    transcript_path=None,
    subagent=None,
    model_provider="openai",
    flat_agent_id=None,
    flat_agent_type=None,
    account_id=None,
    rate_limits=None,
    ordinary_usage_allowed=None,
    inherited_model_provider=None,
    requested_backend=None,
    spawn_model_explicit=False,
):
    source = io.StringIO(
        json.dumps(
            {
                "session_id": "same-thread",
                "turn_id": "turn-1",
                "model": model,
                "model_provider": model_provider,
                "inherited_model_provider": inherited_model_provider,
                "requested_backend": requested_backend,
                "spawn_model_explicit": spawn_model_explicit,
                "prompt": prompt,
                "subagent": subagent,
                "agent_id": flat_agent_id,
                "agent_type": flat_agent_type,
                "account_id": account_id,
                "rate_limits": rate_limits,
                "ordinary_usage_allowed": ordinary_usage_allowed,
                "transcript_path": str(transcript_path) if transcript_path else None,
            }
        )
    )
    sink = io.StringIO()
    assert codex_user_prompt_submit(source, sink, config=config, store=store) == 0
    return json.loads(sink.getvalue())


def test_prompt_reasoning_effort_override_works_with_backend_and_tier(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    store = AuditStore(tmp_path / "audit.db")

    output = invoke(config, store, "@azure @ultra @smart review the policy boundary")

    specific = output["hookSpecificOutput"]
    assert specific["modelProvider"] == "agentroute-azure"
    assert specific["reasoningEffort"] == "ultra"
    assert specific["stripPromptPrefixBytes"] == len(b"@azure @ultra @smart ")
    assert "REASONING_EFFORT_OVERRIDE" in store.latest("same-thread")["reason_codes"]
    receipt = json.loads(store.latest("same-thread")["selection_receipt"])
    assert receipt["selected"]["reasoning_effort"] == "ultra"
    assert receipt["policy"]["reasoning_effort_override"] == "ultra"


def test_manual_tier_override_offers_nonjudgmental_route_feedback(tmp_path):
    config = default_config()
    config.enabled = True
    store = AuditStore(tmp_path / "audit.db")

    invoke(config, store, "routine work")
    previous = store.latest("same-thread")
    output = invoke(config, store, "@normal continue")

    assert previous is not None
    message = output["hookSpecificOutput"]["routeMessage"]
    assert f"previous #{previous['id']}" in message
    assert "too-low|too-high|changed-task|correct" in message


def write_profile_auth(home, account_id):
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(
        json.dumps({"tokens": {"account_id": account_id}}), encoding="utf-8"
    )


def test_healthy_gpt_turn_uses_account_matched_profile_model(tmp_path, monkeypatch):
    personal_home = tmp_path / "personal"
    work_home = tmp_path / "work"
    write_profile_auth(personal_home, "personal-account")
    write_profile_auth(work_home, "work-account")
    config = default_config()
    config.enabled = True
    config.capacity.enabled = True
    config.capacity.profiles = {
        "personal": SubscriptionProfileConfig(
            codex_home=str(personal_home),
            priority=10,
            tiers={"max": {"model": "gpt-6-astra", "reasoning_effort": "high"}},
        ),
        "work": SubscriptionProfileConfig(
            codex_home=str(work_home),
            priority=20,
            tiers={"max": {"model": "gpt-5.6-sol", "reasoning_effort": "high"}},
        ),
    }

    monkeypatch.setenv("CODEX_HOME", str(personal_home))
    personal = invoke(
        config,
        AuditStore(tmp_path / "personal.db"),
        "@gpt @max hi",
        account_id="personal-account",
    )
    monkeypatch.setenv("CODEX_HOME", str(work_home))
    work_store = AuditStore(tmp_path / "work.db")
    work = invoke(
        config,
        work_store,
        "@gpt @max hi",
        account_id="work-account",
    )

    assert personal["hookSpecificOutput"]["model"] == "gpt-6-astra"
    assert work["hookSpecificOutput"]["model"] == "gpt-5.6-sol"
    assert "chatgptProfileHome" not in personal["hookSpecificOutput"]
    assert "chatgptProfileHome" not in work["hookSpecificOutput"]
    assert "stripProviderState" not in personal["hookSpecificOutput"]
    receipt = json.loads(work_store.latest("same-thread")["selection_receipt"])
    assert receipt["selected"]["model"] == "gpt-5.6-sol"
    assert receipt["chatgpt_account"]["source"] == "account_match"
    assert "work-account" not in json.dumps(receipt)


def test_codex_home_disambiguates_two_profiles_with_same_account(tmp_path, monkeypatch):
    personal_home = tmp_path / "personal"
    work_home = tmp_path / "work"
    write_profile_auth(personal_home, "same-account")
    write_profile_auth(work_home, "same-account")
    monkeypatch.setenv("CODEX_HOME", str(work_home))
    config = default_config()
    config.enabled = True
    config.capacity.enabled = True
    config.capacity.profiles = {
        "personal": SubscriptionProfileConfig(
            codex_home=str(personal_home),
            priority=10,
            tiers={"max": {"model": "gpt-6-astra", "reasoning_effort": "high"}},
        ),
        "work": SubscriptionProfileConfig(
            codex_home=str(work_home),
            priority=20,
            tiers={"max": {"model": "gpt-5.6-sol", "reasoning_effort": "high"}},
        ),
    }

    output = invoke(
        config,
        AuditStore(tmp_path / "audit.db"),
        "@gpt @max hi",
        account_id="same-account",
    )

    assert output["hookSpecificOutput"]["model"] == "gpt-5.6-sol"
    assert "ACCOUNT ROUTE · work · account match" in output["hookSpecificOutput"][
        "routeMessage"
    ]


def test_unmatched_gpt_account_keeps_global_model(tmp_path):
    personal_home = tmp_path / "personal"
    write_profile_auth(personal_home, "personal-account")
    config = default_config()
    config.enabled = True
    config.capacity.enabled = True
    config.capacity.profiles["personal"] = SubscriptionProfileConfig(
        codex_home=str(personal_home),
        tiers={"max": {"model": "profile-only-max", "reasoning_effort": "high"}},
    )

    output = invoke(
        config,
        AuditStore(tmp_path / "audit.db"),
        "@gpt @max hi",
        account_id="unknown-account",
    )

    assert output["hookSpecificOutput"]["model"] == "gpt-6-astra"
    assert "ACCOUNT ROUTE · default · account match" in output["hookSpecificOutput"][
        "routeMessage"
    ]


def test_profile_model_compatibility_does_not_require_capacity_failover(tmp_path, monkeypatch):
    work_home = tmp_path / "work"
    write_profile_auth(work_home, "work-account")
    monkeypatch.setenv("CODEX_HOME", str(work_home))
    config = default_config()
    config.enabled = True
    config.capacity.enabled = False
    config.capacity.profiles["work"] = SubscriptionProfileConfig(
        codex_home=str(work_home),
        tiers={"max": {"model": "gpt-5.6-sol", "reasoning_effort": "high"}},
    )

    output = invoke(
        config,
        AuditStore(tmp_path / "audit.db"),
        "@gpt @max hi",
        account_id="work-account",
    )

    assert output["hookSpecificOutput"]["model"] == "gpt-5.6-sol"


def test_authoritative_subscription_lock_falls_back_with_visible_message(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.capacity.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"

    output = invoke(
        config,
        AuditStore(tmp_path / "audit.db"),
        "check status",
        account_id="never-store-this-account-id",
        ordinary_usage_allowed=False,
    )

    specific = output["hookSpecificOutput"]
    assert output["continue"] is True
    assert specific["modelProvider"] == "agentroute-azure"
    assert specific["stripProviderState"] is True
    assert "CAPACITY FALLBACK" in specific["routeMessage"]
    assert "using azure" in specific["routeMessage"]


def test_explicit_subscription_lock_fails_closed_with_auto_guidance(tmp_path):
    config = default_config()
    config.enabled = True
    config.capacity.enabled = True

    output = invoke(
        config,
        AuditStore(tmp_path / "audit.db"),
        "@gpt check status",
        ordinary_usage_allowed=False,
    )

    assert output["continue"] is False
    assert "@auto" in output["systemMessage"]
    assert "Configure another signed-in ChatGPT profile" in output["systemMessage"]


def test_exhausted_subscription_fails_over_to_another_profile_in_place(tmp_path):
    config = default_config()
    config.enabled = True
    config.capacity.enabled = True
    config.capacity.active_profile = "personal"
    config.capacity.profiles = {
        "personal": SubscriptionProfileConfig(
            codex_home=str(tmp_path / "personal"), priority=10
        ),
        "work": SubscriptionProfileConfig(
            codex_home=str(tmp_path / "work"),
            priority=20,
            tiers={"normal": {"model": "work-compatible", "reasoning_effort": "medium"}},
        ),
    }
    statuses = (
        ProfileStatus(
            "personal",
            str(tmp_path / "personal"),
            10,
            True,
            True,
            "personal-hash",
            CapacityState("gpt", "exhausted", "subscription exhausted", "subscription"),
        ),
        ProfileStatus(
            "work",
            str(tmp_path / "work"),
            20,
            True,
            True,
            "work-hash",
            CapacityState(
                "gpt",
                "healthy",
                "subscription 20% used / 80% remaining",
                "subscription",
                20,
            ),
        ),
    )
    store = AuditStore(tmp_path / "audit.db")

    with patch("agentroute.profiles.probe_profiles", return_value=statuses):
        output = invoke(
            config,
            store,
            "continue",
            account_id="personal-account",
            ordinary_usage_allowed=False,
        )

    specific = output["hookSpecificOutput"]
    assert output["continue"] is True
    assert specific["modelProvider"] == "openai"
    assert specific["model"] == "work-compatible"
    assert specific["chatgptProfileHome"] == str(tmp_path / "work")
    assert specific["stripProviderState"] is True
    assert "◆ ACCOUNT FAILOVER · personal → work" in specific["routeMessage"]
    row = store.latest("same-thread")
    receipt = json.loads(row["selection_receipt"])
    assert receipt["chatgpt_account"] == {
        "account_hash": "work-hash",
        "name": "work",
        "source": "failover",
    }
    assert receipt["selected"]["model"] == "work-compatible"
    assert "personal-account" not in json.dumps(dict(row))


def test_selected_subscription_profile_stays_sticky_for_followup_turns(tmp_path):
    config = default_config()
    config.enabled = True
    config.capacity.enabled = True
    config.capacity.active_profile = "personal"
    config.capacity.profiles = {
        "personal": SubscriptionProfileConfig(
            codex_home=str(tmp_path / "personal"), priority=10
        ),
        "work": SubscriptionProfileConfig(codex_home=str(tmp_path / "work"), priority=20),
    }
    exhausted = ProfileStatus(
        "personal",
        str(tmp_path / "personal"),
        10,
        True,
        True,
        "personal-hash",
        CapacityState("gpt", "exhausted", "subscription exhausted", "subscription"),
    )
    work = ProfileStatus(
        "work",
        str(tmp_path / "work"),
        20,
        True,
        True,
        "work-hash",
        CapacityState(
            "gpt",
            "healthy",
            "subscription 20% used / 80% remaining",
            "subscription",
            20,
        ),
    )
    store = AuditStore(tmp_path / "audit.db")

    with patch("agentroute.profiles.probe_profiles", return_value=(exhausted, work)):
        invoke(
            config,
            store,
            "continue",
            account_id="personal-account",
            ordinary_usage_allowed=False,
        )
    with patch("agentroute.profiles.probe_profile", return_value=work):
        followup = invoke(
            config,
            store,
            "status?",
            account_id="personal-account",
            ordinary_usage_allowed=False,
        )

    specific = followup["hookSpecificOutput"]
    assert followup["continue"] is True
    assert specific["chatgptProfileHome"] == str(tmp_path / "work")
    assert specific["stripProviderState"] is True
    assert "◆ ACCOUNT ROUTE · work · session affinity" in specific["routeMessage"]


def test_profile_failover_skips_another_home_for_the_same_account(tmp_path):
    config = default_config()
    config.enabled = True
    config.capacity.enabled = True
    config.capacity.active_profile = "personal"
    config.capacity.profiles = {
        "personal": SubscriptionProfileConfig(
            codex_home=str(tmp_path / "personal"), priority=10
        ),
        "duplicate": SubscriptionProfileConfig(
            codex_home=str(tmp_path / "duplicate"), priority=20
        ),
        "work": SubscriptionProfileConfig(codex_home=str(tmp_path / "work"), priority=30),
    }
    statuses = (
        ProfileStatus(
            "personal",
            str(tmp_path / "personal"),
            10,
            True,
            True,
            "personal-hash",
            CapacityState("gpt", "exhausted", "subscription exhausted", "subscription"),
        ),
        ProfileStatus(
            "duplicate",
            str(tmp_path / "duplicate"),
            20,
            True,
            True,
            "personal-hash",
            CapacityState("gpt", "healthy", "subscription healthy", "subscription", 20),
        ),
        ProfileStatus(
            "work",
            str(tmp_path / "work"),
            30,
            True,
            True,
            "work-hash",
            CapacityState("gpt", "healthy", "subscription healthy", "subscription", 30),
        ),
    )

    with patch("agentroute.profiles.probe_profiles", return_value=statuses):
        output = invoke(
            config,
            AuditStore(tmp_path / "audit.db"),
            "continue",
            account_id="personal-account",
            ordinary_usage_allowed=False,
        )

    specific = output["hookSpecificOutput"]
    assert specific["chatgptProfileHome"] == str(tmp_path / "work")
    assert "◆ ACCOUNT FAILOVER · personal → work" in specific["routeMessage"]


def test_profile_failover_reports_unavailable_current_profile(tmp_path):
    config = default_config()
    config.enabled = True
    config.capacity.enabled = True
    config.capacity.active_profile = "personal"
    config.capacity.profiles = {
        "personal": SubscriptionProfileConfig(
            codex_home=str(tmp_path / "personal"), priority=10
        ),
        "work": SubscriptionProfileConfig(codex_home=str(tmp_path / "work"), priority=20),
    }
    statuses = (
        ProfileStatus(
            "personal",
            str(tmp_path / "personal"),
            10,
            True,
            False,
            "personal-hash",
            CapacityState("gpt", "unavailable", "profile is not signed in", "profile"),
        ),
        ProfileStatus(
            "work",
            str(tmp_path / "work"),
            20,
            True,
            True,
            "work-hash",
            CapacityState("gpt", "healthy", "subscription healthy", "subscription", 20),
        ),
    )

    with patch("agentroute.profiles.probe_profiles", return_value=statuses):
        output = invoke(
            config,
            AuditStore(tmp_path / "audit.db"),
            "continue",
            ordinary_usage_allowed=False,
        )

    assert "current subscription unavailable" in output["hookSpecificOutput"]["routeMessage"]


def test_unknown_subscription_telemetry_does_not_trigger_profile_switch(tmp_path):
    config = default_config()
    config.enabled = True
    config.capacity.enabled = True
    config.capacity.active_profile = "personal"
    config.capacity.profiles = {
        "personal": SubscriptionProfileConfig(
            codex_home=str(tmp_path / "personal"), priority=10
        ),
        "work": SubscriptionProfileConfig(codex_home=str(tmp_path / "work"), priority=20),
    }

    with patch("agentroute.profiles.probe_profiles") as probe:
        output = invoke(config, AuditStore(tmp_path / "audit.db"), "continue")

    probe.assert_not_called()
    assert "chatgptProfileHome" not in output["hookSpecificOutput"]


def test_reviewer_fallbacks_only_include_healthy_distinct_accounts(tmp_path):
    config = default_config()
    config.capacity.enabled = True
    config.capacity.profiles = {
        "current": SubscriptionProfileConfig(
            codex_home=str(tmp_path / "current"), priority=10
        ),
        "duplicate": SubscriptionProfileConfig(
            codex_home=str(tmp_path / "duplicate"), priority=20
        ),
        "healthy": SubscriptionProfileConfig(
            codex_home=str(tmp_path / "healthy"), priority=30
        ),
        "exhausted": SubscriptionProfileConfig(
            codex_home=str(tmp_path / "exhausted"), priority=40
        ),
    }
    current = ProfileStatus(
        "current",
        str(tmp_path / "current"),
        10,
        True,
        True,
        "current-account",
        CapacityState("gpt", "healthy", "healthy", "subscription", 10),
    )
    statuses = (
        current,
        ProfileStatus(
            "duplicate",
            str(tmp_path / "duplicate"),
            20,
            True,
            True,
            "current-account",
            CapacityState("gpt", "healthy", "healthy", "subscription", 10),
        ),
        ProfileStatus(
            "healthy",
            str(tmp_path / "healthy"),
            30,
            True,
            True,
            "other-account",
            CapacityState("gpt", "warning", "warning", "subscription", 90),
        ),
        ProfileStatus(
            "exhausted",
            str(tmp_path / "exhausted"),
            40,
            True,
            True,
            "third-account",
            CapacityState("gpt", "exhausted", "exhausted", "subscription", 100),
        ),
    )

    with patch("agentroute.profiles.probe_profiles", return_value=statuses):
        fallbacks = reviewer_fallback_profiles(config, current)

    assert [profile.name for profile in fallbacks] == ["healthy"]


def test_gpt_turn_emits_safe_reviewer_profile_fallback_metadata(tmp_path, monkeypatch):
    current_home = tmp_path / "current"
    fallback_home = tmp_path / "fallback"
    write_profile_auth(current_home, "current-account")
    write_profile_auth(fallback_home, "fallback-account")
    monkeypatch.setenv("CODEX_HOME", str(current_home))
    config = default_config()
    config.enabled = True
    config.capacity.enabled = True
    config.capacity.profiles = {
        "current": SubscriptionProfileConfig(codex_home=str(current_home), priority=10),
        "fallback": SubscriptionProfileConfig(codex_home=str(fallback_home), priority=20),
    }
    statuses = (
        ProfileStatus(
            "current",
            str(current_home),
            10,
            True,
            True,
            "current-hash",
            CapacityState("gpt", "healthy", "healthy", "subscription", 10),
        ),
        ProfileStatus(
            "fallback",
            str(fallback_home),
            20,
            True,
            True,
            "fallback-hash",
            CapacityState("gpt", "healthy", "healthy", "subscription", 20),
        ),
    )

    with patch("agentroute.profiles.probe_profiles", return_value=statuses):
        output = invoke(
            config,
            AuditStore(tmp_path / "audit.db"),
            "check status",
            account_id="current-account",
            rate_limits={"primary": {"usedPercent": 10}},
        )

    specific = output["hookSpecificOutput"]
    assert specific["reviewerProfileName"] == "current"
    assert specific["reviewerFallbackProfiles"] == [
        {"name": "fallback", "codexHome": str(fallback_home)}
    ]
    assert "current-account" not in json.dumps(specific)
    assert "fallback-account" not in json.dumps(specific)


def test_non_gpt_turn_does_not_emit_reviewer_profile_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.capacity.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"

    output = invoke(
        config,
        AuditStore(tmp_path / "audit.db"),
        "@azure check status",
    )

    specific = output["hookSpecificOutput"]
    assert "reviewerProfileName" not in specific
    assert "reviewerFallbackProfiles" not in specific


def test_observe_mode_does_not_emit_override(tmp_path):
    config = default_config()
    output = invoke(config, AuditStore(tmp_path / "audit.db"), "@smart investigate")
    specific = output["hookSpecificOutput"]

    assert "Would select SMART" in specific["additionalContext"]
    assert "model" not in specific
    assert "reasoningEffort" not in specific


def test_enabled_mode_emits_native_override_and_keeps_session_history(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTROUTE_RUNTIME_BUILD_ID", raising=False)
    config = default_config()
    config.enabled = True
    store = AuditStore(tmp_path / "audit.db")

    first = invoke(config, store, "@smart investigate")
    second = invoke(config, store, "go ahead")

    assert first["hookSpecificOutput"]["model"] == "gpt-6-sol"
    assert first["hookSpecificOutput"]["reasoningEffort"] == "high"
    assert first["hookSpecificOutput"]["routeMessage"] == (
        "◆ ACCOUNT ROUTE · default · account match\n"
        "◆ MODEL ROUTE · SMART → gpt-6-sol · high reasoning "
        "· backend gpt/openai · scope root · source MANUAL "
        "· rule confidence 100% · rule score -0.5"
    )
    assert second["hookSpecificOutput"]["model"] == "gpt-6-sol"
    assert len(store.history("same-thread")) == 2


def test_explicit_backend_is_sticky_and_directive_is_hidden_from_model(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    store = AuditStore(tmp_path / "audit.db")

    first = invoke(config, store, "@azure check status and eta")
    second = invoke(config, store, "I wanted you to check the status of the CRM sync")

    assert first["hookSpecificOutput"]["modelProvider"] == "agentroute-azure"
    assert first["hookSpecificOutput"]["stripProviderState"] is True
    assert first["hookSpecificOutput"]["stripPromptPrefixBytes"] == len("@azure ")
    assert second["hookSpecificOutput"]["modelProvider"] == "agentroute-azure"
    assert second["hookSpecificOutput"]["model"] == first["hookSpecificOutput"]["model"]
    assert second["hookSpecificOutput"]["stripProviderState"] is True
    assert "stripPromptPrefixBytes" not in second["hookSpecificOutput"]
    assert "SESSION_AFFINITY" in store.latest("same-thread")["reason_codes"]


def test_custom_backend_prefix_is_sticky_and_hidden_from_model(tmp_path):
    config = default_config()
    config.enabled = True
    config.backends["ollama"] = ExecutionBackendConfig(
        enabled=True,
        codex_provider="agentroute-ollama",
        display_name="Local Ollama",
        base_url="http://127.0.0.1:11434/v1",
        tool_compatibility="functions_and_apply_patch",
        tiers={
            tier: ModelTarget(model="qwen3-coder")
            for tier in ("fast", "normal", "smart", "max")
        },
    )
    store = AuditStore(tmp_path / "audit.db")

    first = invoke(config, store, "@ollama check status and eta")
    second = invoke(config, store, "continue with the check")

    assert first["hookSpecificOutput"]["modelProvider"] == "agentroute-ollama"
    assert first["hookSpecificOutput"]["stripPromptPrefixBytes"] == len("@ollama ")
    assert second["hookSpecificOutput"]["modelProvider"] == "agentroute-ollama"


def test_auto_clears_explicit_backend_affinity(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    store = AuditStore(tmp_path / "audit.db")

    invoke(config, store, "@azure say hi")
    cleared = invoke(config, store, "@auto say hi")
    followup = invoke(config, store, "say hi again")

    assert cleared["hookSpecificOutput"]["modelProvider"] == "openai"
    assert followup["hookSpecificOutput"]["modelProvider"] == "openai"


def test_backend_affinity_is_scoped_away_from_subagents(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    store = AuditStore(tmp_path / "audit.db")

    invoke(config, store, "@azure say hi")
    child = invoke(
        config,
        store,
        "say hi",
        subagent={"agent_id": "/root/worker", "agent_type": "worker"},
    )

    assert child["hookSpecificOutput"]["modelProvider"] == "openai"


def test_route_message_identifies_managed_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AGENTROUTE_RUNTIME_BUILD_ID",
        "upstream-commit-provider-routing-v8",
    )
    config = default_config()
    config.enabled = True

    output = invoke(config, AuditStore(tmp_path / "audit.db"), "@fast say hi")

    assert output["hookSpecificOutput"]["routeMessage"].endswith(" · runtime v8")


def test_subagent_task_is_independently_routed_and_audited(tmp_path):
    config = default_config()
    config.enabled = True
    store = AuditStore(tmp_path / "audit.db")

    output = invoke(
        config,
        store,
        "Rename the internal label and fix the typo",
        subagent={"agent_id": "/root/mechanical", "agent_type": "worker"},
    )
    row = store.latest("same-thread")

    assert output["hookSpecificOutput"]["model"] == "gpt-6-luna"
    assert output["hookSpecificOutput"]["modelProvider"] == "openai"
    assert "scope subagent" in output["hookSpecificOutput"]["routeMessage"]
    assert row["route_scope"] == "subagent"
    assert row["agent_id"] == "/root/mechanical"


def test_first_subagent_turn_inherits_azure_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    store = AuditStore(tmp_path / "audit.db")

    output = invoke(
        config,
        store,
        "Investigate the allocator state machine",
        model="dev-gpt-5.6-sol",
        model_provider="agentroute-azure",
        flat_agent_id="child-azure",
    )
    row = store.latest("same-thread")

    assert output["hookSpecificOutput"]["modelProvider"] == "agentroute-azure"
    assert row["backend"] == "azure"
    assert row["sticky_backend"] is None
    assert "PROVIDER_INHERITANCE" in row["reason_codes"]


def test_first_subagent_turn_inherits_openai_provider(tmp_path):
    config = default_config()
    config.enabled = True
    store = AuditStore(tmp_path / "audit.db")

    output = invoke(
        config,
        store,
        "Investigate the allocator state machine",
        model_provider="openai",
        flat_agent_id="child-gpt",
    )
    row = store.latest("same-thread")

    assert output["hookSpecificOutput"]["modelProvider"] == "openai"
    assert row["backend"] == "gpt"
    assert "PROVIDER_INHERITANCE" in row["reason_codes"]


def test_subagent_explicit_override_can_cross_provider_without_changing_parent(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    config.backends["deepseek"].enabled = True
    store = AuditStore(tmp_path / "audit.db")

    invoke(config, store, "@azure investigate this", model_provider="openai")
    child_a = invoke(
        config,
        store,
        "@deepseek investigate this",
        model_provider="agentroute-azure",
        flat_agent_id="child-a",
    )
    child_b = invoke(
        config,
        store,
        "investigate this",
        model="gpt-5",
        model_provider="agentroute-azure",
        flat_agent_id="child-b",
    )

    assert child_a["hookSpecificOutput"]["modelProvider"] == "agentroute-deepseek"
    assert child_b["hookSpecificOutput"]["modelProvider"] == "agentroute-azure"
    assert store.route_preference("same-thread", "subagent", "child-a") == "deepseek"
    assert store.route_preference("same-thread", "subagent", "child-b") is None
    assert store.route_preference("same-thread", "root", None) == "azure"


def test_existing_child_affinity_wins_over_inherited_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.backends["deepseek"].enabled = True
    store = AuditStore(tmp_path / "audit.db")

    invoke(
        config,
        store,
        "@deepseek investigate this",
        model_provider="agentroute-azure",
        flat_agent_id="child-a",
    )
    followup = invoke(
        config,
        store,
        "continue investigating",
        model_provider="agentroute-azure",
        flat_agent_id="child-a",
    )

    assert followup["hookSpecificOutput"]["modelProvider"] == "agentroute-deepseek"


def test_explicit_subagent_model_uses_its_uniquely_configured_backend(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"

    output = invoke(
        config,
        AuditStore(tmp_path / "audit.db"),
        "Investigate this implementation",
        model="gpt-6-astra",
        model_provider="agentroute-azure",
        spawn_model_explicit=True,
        flat_agent_id="child-a",
    )

    assert output["hookSpecificOutput"]["modelProvider"] == "openai"
    assert output["hookSpecificOutput"]["model"] == "gpt-6-astra"


def test_explicit_subagent_model_can_cross_from_inherited_azure_to_gpt(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"

    output = invoke(
        config,
        AuditStore(tmp_path / "audit.db"),
        "Review the implementation",
        model="gpt-6-sol",
        model_provider="agentroute-azure",
        spawn_model_explicit=True,
        flat_agent_id="child-gpt",
    )

    assert output["hookSpecificOutput"]["modelProvider"] == "openai"
    assert output["hookSpecificOutput"]["model"] == "gpt-6-sol"


def test_ambiguous_subagent_model_preserves_inherited_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    config.backends["azure"].tiers["smart"] = ModelTarget(
        model="gpt-5.6-sol", reasoning_effort="high"
    )

    output = invoke(
        config,
        AuditStore(tmp_path / "audit.db"),
        "Review the implementation",
        model="gpt-5.6-sol",
        model_provider="agentroute-azure",
        spawn_model_explicit=True,
        flat_agent_id="child-azure",
    )

    assert output["hookSpecificOutput"]["modelProvider"] == "agentroute-azure"


def test_subagent_uses_explicit_inherited_provider_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"

    output = invoke(
        config,
        AuditStore(tmp_path / "audit.db"),
        "Review the implementation",
        model="gpt-5.6-sol",
        model_provider="openai",
        inherited_model_provider="agentroute-azure",
        flat_agent_id="child-azure",
    )

    assert output["hookSpecificOutput"]["modelProvider"] == "agentroute-azure"


def test_subagent_explicit_backend_crosses_provider_and_becomes_sticky(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    config.backends["deepseek"].enabled = True
    store = AuditStore(tmp_path / "audit.db")

    first = invoke(
        config,
        store,
        "Review the implementation",
        model="dev-gpt-5.6-sol",
        model_provider="agentroute-azure",
        inherited_model_provider="agentroute-azure",
        requested_backend="deepseek",
        flat_agent_id="child-deepseek",
    )
    followup = invoke(
        config,
        store,
        "Continue",
        model_provider="agentroute-azure",
        inherited_model_provider="agentroute-azure",
        flat_agent_id="child-deepseek",
    )

    assert first["hookSpecificOutput"]["modelProvider"] == "agentroute-deepseek"
    assert followup["hookSpecificOutput"]["modelProvider"] == "agentroute-deepseek"
    assert store.route_preference("same-thread", "subagent", "child-deepseek") == "deepseek"
    assert "SPAWN_BACKEND_OVERRIDE" in store.rows_since()[0]["reason_codes"]


def test_inherited_model_is_not_mistaken_for_explicit_provider_jump(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"

    output = invoke(
        config,
        AuditStore(tmp_path / "audit.db"),
        "Review the implementation",
        model="gpt-5.6-sol",
        model_provider="openai",
        inherited_model_provider="agentroute-azure",
        spawn_model_explicit=False,
        flat_agent_id="child-azure",
    )

    assert output["hookSpecificOutput"]["modelProvider"] == "agentroute-azure"


def test_unknown_spawn_backend_is_blocked_without_silent_gpt_fallback(tmp_path):
    config = default_config()
    config.enabled = True

    output = invoke(
        config,
        AuditStore(tmp_path / "audit.db"),
        "Review the implementation",
        requested_backend="missing-provider",
        flat_agent_id="child-invalid",
    )

    assert output["continue"] is False
    assert "explicit child backend missing-provider is unavailable" in output["stopReason"]


def test_explicit_spawn_model_must_exist_on_requested_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    config.backends["deepseek"].enabled = True

    output = invoke(
        config,
        AuditStore(tmp_path / "audit.db"),
        "Review the implementation",
        model="gpt-6-astra",
        model_provider="agentroute-azure",
        inherited_model_provider="agentroute-azure",
        requested_backend="deepseek",
        spawn_model_explicit=True,
        flat_agent_id="child-invalid-model",
    )

    assert output["continue"] is False
    assert "explicit child model gpt-6-astra is not configured for backend deepseek" in output[
        "stopReason"
    ]


def test_root_turn_ignores_spawn_only_backend_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    config = default_config()
    config.enabled = True
    config.backends["deepseek"].enabled = True

    output = invoke(
        config,
        AuditStore(tmp_path / "audit.db"),
        "Review the implementation",
        requested_backend="deepseek",
    )

    assert output["hookSpecificOutput"]["modelProvider"] == "openai"


def test_flat_codex_subagent_fields_are_routed_and_audited(tmp_path):
    config = default_config()
    config.enabled = True
    store = AuditStore(tmp_path / "audit.db")

    output = invoke(
        config,
        store,
        "Design a zero-downtime database migration",
        flat_agent_id="0199-child",
        flat_agent_type="worker",
    )
    row = store.latest("same-thread")

    assert "scope subagent" in output["hookSpecificOutput"]["routeMessage"]
    assert row["route_scope"] == "subagent"
    assert row["agent_id"] == "0199-child"


def test_opaque_subagent_followup_inherits_its_own_route(tmp_path):
    config = default_config()
    config.enabled = True
    store = AuditStore(tmp_path / "audit.db")

    invoke(config, store, "@smart architecture review", flat_agent_id="child-a")
    invoke(config, store, "@fast rename a label", flat_agent_id="child-b")
    child_a_followup = invoke(
        config,
        store,
        "",
        model="gpt-5.6-sol",
        flat_agent_id="child-a",
    )
    child_b_followup = invoke(
        config,
        store,
        "",
        model="gpt-5.6-luna",
        flat_agent_id="child-b",
    )

    assert child_a_followup["hookSpecificOutput"]["model"] == "gpt-6-sol"
    assert child_b_followup["hookSpecificOutput"]["model"] == "gpt-6-luna"
    child_rows = [row for row in store.history("same-thread") if row["agent_id"]]
    assert child_rows[0]["classification_source"] == "session_affinity"
    assert child_rows[1]["classification_source"] == "session_affinity"
    assert child_rows[0]["agent_id"] == "child-b"
    assert child_rows[1]["agent_id"] == "child-a"


def test_confirmation_uses_previous_assistant_task_definition(tmp_path):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                "Redesign the authentication architecture and perform a "
                                "zero-downtime database migration across the services."
                            ),
                        }
                    ],
                },
            }
        )
        + "\n"
    )
    config = default_config()
    config.enabled = True
    store = AuditStore(tmp_path / "audit.db")

    output = invoke(config, store, "ok do it", transcript_path=transcript)
    row = store.latest("same-thread")

    assert output["hookSpecificOutput"]["model"] == "gpt-6-sol"
    assert "MAX→SMART SAFETY FALLBACK" in output["hookSpecificOutput"]["routeMessage"]
    assert row is not None
    assert row["task_context_used"] == 1
    assert "TASK_DEFINITION_INHERITANCE" in row["reason_codes"]


def test_credential_route_notice_is_visible(tmp_path):
    config = default_config()
    config.enabled = True

    output = invoke(
        config,
        AuditStore(tmp_path / "audit.db"),
        "service api key: abcdefghijklmnop1234",
    )

    assert output["hookSpecificOutput"]["model"] == "gpt-6-sol"
    assert "CREDENTIAL RISK" in output["hookSpecificOutput"]["routeMessage"]


def test_hook_surfaces_llm_classifier_confidence_and_audits_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agentroute.classifier.urllib.request.urlopen",
        lambda request, timeout: FakeClassifierResponse(),
    )
    config = default_config()
    config.enabled = True
    config.routing.classifier.enabled = True
    config.routing.classifier.endpoint = "http://127.0.0.1:11434/v1/chat/completions"
    store = AuditStore(tmp_path / "audit.db")

    output = invoke(config, store, "Please handle this")
    row = store.latest("same-thread")

    assert output["hookSpecificOutput"]["model"] == "gpt-6-sol"
    assert "classifier confidence 87%" in output["hookSpecificOutput"]["routeMessage"]
    assert "rule score -0.5" in output["hookSpecificOutput"]["routeMessage"]
    assert "implementation" in output["hookSpecificOutput"]["routeMessage"]
    assert row is not None
    assert row["classification_source"] == "local_llm"
    assert row["classifier_task_type"] == "implementation"
    assert row["classifier_reasoning_effort"] == "high"
    assert row["reasoning_effort_source"] == "classifier"
    assert len(row["classifier_reason_hash"]) == 64
    assert "substantial implementation" not in str(dict(row)).lower()
    assert row["classifier_latency_ms"] is not None
    assert len(row["classifier_request_hash"]) == 64
    assert json.loads(row["classifier_usage"])["total_tokens"] == 120


def test_jev_shadow_is_audited_but_cannot_change_a_live_route(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agentroute.classifier.urllib.request.urlopen",
        lambda request, timeout: FakeClassifierResponse()
        if request.full_url.endswith("/chat/completions")
        else type("JevResponse", (), {
            "__enter__": lambda self: self,
            "__exit__": lambda self, *args: None,
            "read": lambda self: json.dumps({
                "model": "nli-deberta-large",
                "answers": {
                    "tier": {"noul": 0.95},
                    "requires_smart": {"noul": 0.05},
                    "requires_max": {"noul": 0.01},
                    "reasoning_effort": {"choice": "low", "confidence": 0.9},
                    "task_type": {"choice": "formatting", "confidence": 0.9},
                },
            }).encode(),
        })(),
    )
    config = default_config()
    config.enabled = True
    config.routing.classifier.enabled = True
    config.routing.classifier.endpoint = "http://127.0.0.1:11434/v1/chat/completions"
    config.routing.classifier.jev_shadow.enabled = True
    store = AuditStore(tmp_path / "audit.db")

    output = invoke(config, store, "Please handle this")
    receipt = json.loads(store.latest("same-thread")["selection_receipt"])

    assert output["hookSpecificOutput"]["model"] == "gpt-6-sol"
    assert receipt["shadow_jev"]["status"] == "succeeded"
    assert receipt["shadow_jev"]["tier"] == "fast"
    assert receipt["shadow_jev"]["agreement"]["selected_tier"] is False


def test_llm_context_telemetry_distinguishes_sent_from_inherited(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agentroute.classifier.urllib.request.urlopen",
        lambda request, timeout: FakeClassifierResponse(),
    )
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Verify the production records against the source system.",
                        }
                    ],
                },
            }
        )
        + "\n"
    )
    config = default_config()
    config.enabled = True
    config.routing.classifier.enabled = True
    config.routing.classifier.endpoint = "http://127.0.0.1:11434/v1/chat/completions"
    store = AuditStore(tmp_path / "audit.db")

    invoke(config, store, "ok check?", transcript_path=transcript)
    row = store.latest("same-thread")
    receipt = json.loads(row["selection_receipt"])

    assert row["previous_context_sent"] == 1
    assert row["resolved_task_inherited"] == 0
    assert row["task_context_used"] == 1
    assert receipt["context"] == {
        "previous_context_sent": True,
        "resolved_task_inherited": False,
    }
    assert receipt["classifier"]["request_hash"] == row["classifier_request_hash"]


def test_explicit_confirmation_approves_agent_request(tmp_path):
    transcript = tmp_path / "rollout.jsonl"
    assistant_text = (
        "This needs a stronger review.\n\n"
        "MODEL_REQUEST: SMART\n"
        "MODEL_REQUEST_REASON: The production failure spans several services."
    )
    transcript.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": assistant_text}],
                },
            }
        )
        + "\n"
    )
    config = default_config()
    config.enabled = True
    store = AuditStore(tmp_path / "audit.db")

    output = invoke(config, store, "ok do it", transcript_path=transcript)
    row = store.latest("same-thread")

    assert "AGENT REQUEST APPROVED" in output["hookSpecificOutput"]["routeMessage"]
    assert output["hookSpecificOutput"]["model"] == "gpt-6-sol"
    assert row is not None
    assert row["agent_requested_tier"] == "smart"
    assert len(row["agent_request_reason_hash"]) == 64
    assert "production failure" not in str(dict(row)).lower()


def test_hook_fails_open_on_invalid_input(tmp_path):
    sink = io.StringIO()

    assert codex_user_prompt_submit(io.StringIO("{}"), sink) == 0
    assert json.loads(sink.getvalue())["continue"] is True


def test_classifier_fallback_is_visible_in_model_line(tmp_path, monkeypatch):
    def unavailable(request, timeout):
        raise TimeoutError("classifier timed out")

    monkeypatch.setattr("agentroute.classifier.urllib.request.urlopen", unavailable)
    config = default_config()
    config.enabled = True
    config.routing.classifier.enabled = True
    config.routing.classifier.endpoint = "http://127.0.0.1:11434/v1/chat/completions"

    output = invoke(config, AuditStore(tmp_path / "audit.db"), "Please handle this")

    assert "CLASSIFIER FALLBACK" in output["hookSpecificOutput"]["routeMessage"]


def test_stop_hook_records_exact_turn_usage(tmp_path):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "token_usage_record",
                "payload": {
                    "turn_id": "turn-1",
                    "turn_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 800,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 50,
                        "reasoning_output_tokens": 10,
                        "total_tokens": 1050,
                    },
                },
            }
        )
        + "\n"
    )
    config = default_config()
    store = AuditStore(tmp_path / "audit.db")
    invoke(config, store, "show status")
    sink = io.StringIO()
    payload = {
        "session_id": "same-thread",
        "turn_id": "turn-1",
        "model": "gpt-6-luna",
        "transcript_path": str(transcript),
    }

    assert codex_stop(io.StringIO(json.dumps(payload)), sink, store=store) == 0
    row = store.latest("same-thread")

    assert json.loads(sink.getvalue()) == {"continue": True, "suppressOutput": True}
    assert row["answer_model"] == "gpt-6-luna"
    assert row["answer_input_tokens"] == 1000
    assert row["answer_cached_input_tokens"] == 800
    assert row["answer_output_tokens"] == 50


def test_stop_hook_records_completion_when_usage_is_missing(tmp_path):
    config = default_config()
    store = AuditStore(tmp_path / "audit.db")
    invoke(config, store, "show status")
    sink = io.StringIO()
    payload = {
        "session_id": "same-thread",
        "turn_id": "turn-1",
        "model": "gpt-5.6-luna",
        "transcript_path": str(tmp_path / "missing.jsonl"),
    }

    assert codex_stop(io.StringIO(json.dumps(payload)), sink, store=store) == 0
    row = store.latest("same-thread")

    assert row["turn_completed_at"] is not None
    assert row["turn_duration_ms"] >= 0
    assert row["usage_recorded_at"] is None


def test_subagent_stop_records_completion_for_flat_codex_fields(tmp_path):
    config = default_config()
    store = AuditStore(tmp_path / "audit.db")
    invoke(
        config,
        store,
        "Design a zero-downtime database migration",
        flat_agent_id="0199-child",
        flat_agent_type="worker",
    )
    sink = io.StringIO()
    payload = {
        "session_id": "same-thread",
        "turn_id": "turn-1",
        "model": "gpt-5.6-luna",
        "agent_id": "0199-child",
        "agent_type": "worker",
        "agent_transcript_path": str(tmp_path / "missing-child.jsonl"),
    }

    assert codex_stop(io.StringIO(json.dumps(payload)), sink, store=store) == 0
    row = store.latest("same-thread")

    assert row["route_scope"] == "subagent"
    assert row["agent_id"] == "0199-child"
    assert row["turn_outcome"] == "completed"
    assert row["usage_status"] == "missing"


def test_subagent_stop_reads_usage_from_child_transcript(tmp_path):
    parent_transcript = tmp_path / "parent.jsonl"
    child_transcript = tmp_path / "child.jsonl"
    parent_transcript.write_text(
        json.dumps(
            {
                "type": "token_usage_record",
                "payload": {
                    "turn_id": "turn-1",
                    "turn_token_usage": {"input_tokens": 9999, "total_tokens": 9999},
                },
            }
        )
        + "\n"
    )
    child_transcript.write_text(
        json.dumps(
            {
                "type": "token_usage_record",
                "payload": {
                    "turn_id": "turn-1",
                    "turn_token_usage": {
                        "input_tokens": 321,
                        "cached_input_tokens": 123,
                        "output_tokens": 45,
                        "reasoning_output_tokens": 6,
                        "total_tokens": 366,
                    },
                },
            }
        )
        + "\n"
    )
    config = default_config()
    store = AuditStore(tmp_path / "audit.db")
    invoke(
        config,
        store,
        "Investigate the distributed-system failure",
        flat_agent_id="0199-child",
        flat_agent_type="worker",
    )
    sink = io.StringIO()
    payload = {
        "session_id": "same-thread",
        "turn_id": "turn-1",
        "model": "gpt-5.6-sol",
        "agent_id": "0199-child",
        "agent_type": "worker",
        "transcript_path": str(parent_transcript),
        "agent_transcript_path": str(child_transcript),
    }

    assert codex_stop(io.StringIO(json.dumps(payload)), sink, store=store) == 0
    row = store.latest("same-thread")

    assert row["route_scope"] == "subagent"
    assert row["agent_id"] == "0199-child"
    assert row["answer_input_tokens"] == 321
    assert row["answer_cached_input_tokens"] == 123
    assert row["answer_output_tokens"] == 45
    assert row["answer_reasoning_output_tokens"] == 6
    assert row["usage_status"] == "recorded"
