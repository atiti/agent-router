import json

from test_hook import invoke

from agentroute.audit import AuditStore
from agentroute.capacity import CapacityState
from agentroute.config import SubscriptionProfileConfig, default_config
from agentroute.profiles import ProfileStatus, TurnProfileSelection


def adaptive_config(monkeypatch):
    config = default_config()
    config.enabled = True
    config.routing.classifier.enabled = False
    config.routing.provider_selection.mode = "adaptive"
    config.routing.provider_selection.candidates = ["gpt", "deepseek"]
    config.backends["deepseek"].enabled = True
    config.capacity.enabled = True
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr("agentroute.hook.select_turn_profile", lambda *a, **kw: None)
    return config


def test_goal_automatic_provider_choice_does_not_become_a_session_pin(tmp_path, monkeypatch):
    config = adaptive_config(monkeypatch)
    store = AuditStore(tmp_path / "audit.sqlite")
    exhausted = False

    def candidate_state(_config, _context, name):
        return CapacityState(
            name, "exhausted" if name == "gpt" and exhausted else "healthy",
            "test capacity", used_percent=100 if name == "gpt" and exhausted else 0,
        )

    monkeypatch.setattr("agentroute.provider_selection.candidate_capacity", candidate_state)
    first = invoke(config, store, "Implement the feature", goal_id="goal-1")
    assert first["continue"]
    assert store.latest()["backend"] == "gpt"
    assert store.route_preference("same-thread") is None

    exhausted = True
    second = invoke(
        config, AuditStore(store.path), "Implement the feature", goal_id="goal-1",
        turn_id="turn-2", model=first["hookSpecificOutput"]["model"],
    )
    assert second["continue"]
    assert store.latest()["backend"] == "deepseek"
    receipt = json.loads(store.latest()["selection_receipt"])
    assert receipt["provider_selection"]["locked_by"] is None
    assert store.route_preference("same-thread") is None


def test_goal_explicit_provider_preference_remains_pinned(tmp_path, monkeypatch):
    config = adaptive_config(monkeypatch)
    store = AuditStore(tmp_path / "audit.sqlite")
    monkeypatch.setattr(
        "agentroute.provider_selection.candidate_capacity",
        lambda _config, _context, name: CapacityState(name, "healthy", "test capacity"),
    )
    first = invoke(config, store, "@deepseek Implement the feature", goal_id="goal-1")
    assert first["continue"]
    assert store.route_preference("same-thread") == "deepseek"

    second = invoke(
        config, AuditStore(store.path), "@deepseek Implement the feature", goal_id="goal-1",
        turn_id="turn-2", model=first["hookSpecificOutput"]["model"],
        model_provider=config.backends["deepseek"].codex_provider,
    )
    assert second["continue"]
    assert store.latest()["backend"] == "deepseek"
    receipt = json.loads(store.latest()["selection_receipt"])
    assert receipt["provider_selection"]["locked_by"] == "explicit_session_preference"


def test_profile_selection_cannot_resurrect_an_excluded_default_gpt(tmp_path, monkeypatch):
    config = adaptive_config(monkeypatch)
    config.routing.provider_selection.candidates = ["deepseek"]
    config.backends["deepseek"].enabled = False
    config.capacity.profiles = {
        "test": SubscriptionProfileConfig(credential_home=str(tmp_path / "profile")),
    }
    healthy = ProfileStatus(
        "test", str(tmp_path / "profile"), 0, True, True, "test-account",
        CapacityState("gpt", "healthy", "healthy account"),
    )
    selection = TurnProfileSelection("test", healthy, healthy, False, "account_match", False)
    monkeypatch.setattr("agentroute.hook.select_turn_profile", lambda *a, **kw: selection)
    store = AuditStore(tmp_path / "audit.sqlite")

    result = invoke(config, store, "Implement the feature")

    assert result["continue"] is False
    assert store.latest()["capacity_blocked"]
    receipt = json.loads(store.latest()["selection_receipt"])
    assert receipt["provider_selection"]["recommended_backend"] is None
    assert receipt["capacity"]["blocked"]


def test_adaptive_selection_uses_healthy_alternate_gpt_account_before_leaving_pool(
    tmp_path, monkeypatch
):
    config = adaptive_config(monkeypatch)
    config.capacity.profiles["alternate"] = SubscriptionProfileConfig(
        credential_home=str(tmp_path / "alternate"),
    )
    healthy = ProfileStatus(
        "alternate", str(tmp_path / "alternate"), 1, True, True, "alternate-account",
        CapacityState("gpt", "healthy", "alternate 10% used", used_percent=10),
    )
    selection = TurnProfileSelection("default", None, healthy, True, "failover", True)
    calls = []

    def choose(*args, **kwargs):
        calls.append(kwargs["tier"])
        return selection

    monkeypatch.setattr("agentroute.hook.select_turn_profile", choose)
    store = AuditStore(tmp_path / "audit.sqlite")
    result = invoke(
        config, store, "Implement the feature",
        rate_limits={"limit_id": "codex", "primary": {"usedPercent": 99}},
    )
    assert result["continue"]
    assert store.latest()["backend"] == "gpt"
    assert len(calls) == 1  # Reuse the account selection, without a second probe.
    receipt = json.loads(store.latest()["selection_receipt"])
    row = next(r for r in receipt["provider_selection"]["candidates"] if r["backend"] == "gpt")
    assert row["subscription_profile"] == "alternate"
    assert row["used_percent"] == 10
