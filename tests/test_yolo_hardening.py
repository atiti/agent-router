"""YOLO review fixes: effort carry-over, allowlist, on-disk format and Azure derivation."""

import pytest
import yaml

from agentroute.config import (
    ModelCapabilities,
    ModelTarget,
    default_config,
    load_config,
    merge_default_yolo_targets,
    save_config,
)
from agentroute.grok_bridge import ensure_backend as ensure_grok_backend
from agentroute.models import RouteContext, Tier
from agentroute.providers import ensure_claude_bridge_backend
from agentroute.router import Router


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("agentroute.provider_selection.local_bridge_ready", lambda *_: True)
    config = default_config()
    config.policy.max_tier = "yolo"
    ensure_claude_bridge_backend(config, 8090)
    ensure_grok_backend(config)
    azure = config.backends["azure"]
    azure.enabled = True
    azure.base_url = "https://example.openai.azure.com/openai/v1"
    azure.tiers["max"] = ModelTarget(
        model="dev-gpt-6-astra", reasoning_effort="high", context_window=872_000
    )
    merge_default_yolo_targets(config)
    return config


def route(config, prompt, **kwargs):
    return Router(config).route(RouteContext(session_id="yolo", latest_prompt=prompt, **kwargs))


def continue_yolo(config, **kwargs):
    return route(
        config,
        "ok continue",
        current_tier=Tier.YOLO,
        previous_task_tier=Tier.YOLO,
        previous_yolo_opt_in="explicit_tag",
        **kwargs,
    )


# --- 1. Effort carry-over across continuations ------------------------------------


def test_preset_effort_is_not_carried_from_grok_to_gpt(config):
    decision = continue_yolo(
        config, previous_reasoning_effort="xhigh", previous_yolo_backend="grok"
    )
    assert (decision.backend, decision.reasoning_effort) == ("gpt", "ultra")


def test_gpt_ultra_is_not_sent_to_grok(config):
    decision = continue_yolo(
        config,
        sticky_backend="grok",
        previous_reasoning_effort="ultra",
        previous_yolo_backend="gpt",
    )
    assert not decision.capacity_blocked
    assert (decision.backend, decision.reasoning_effort) == ("grok", "xhigh")


def test_explicit_effort_is_carried_on_the_same_backend_only(config):
    same = continue_yolo(
        config, previous_yolo_effort="high", previous_yolo_backend="gpt", sticky_backend="gpt"
    )
    assert same.reasoning_effort == "high"
    assert same.selection_receipt["yolo"]["explicit_reasoning_effort"] == "high"
    moved = continue_yolo(
        config, previous_yolo_effort="ultra", previous_yolo_backend="gpt", sticky_backend="grok"
    )
    assert moved.reasoning_effort == "xhigh"
    assert moved.selection_receipt["yolo"]["explicit_reasoning_effort"] is None


def test_explicit_unsupported_effort_is_rejected(config):
    decision = route(config, "@grok @yolo @ultra research this")
    assert decision.capacity_blocked
    assert decision.capacity_trigger == "yolo_unavailable"
    assert "unsupported_effort:grok-4.7/ultra" in (decision.capacity_detail or "")


def test_model_without_reasoning_accepts_no_effort(config):
    config.capabilities.models["gpt-6.2-astra"] = ModelCapabilities(
        tool_calling="full", reasoning=False
    )
    decision = route(config, "@gpt @yolo go")
    assert decision.capacity_blocked
    assert "unsupported_effort:gpt-6.2-astra/ultra" in (decision.capacity_detail or "")


# --- 2. The candidate allowlist is the admission boundary ---------------------------


def test_default_allowlist_never_reaches_azure(config):
    assert "azure" not in config.routing.provider_selection.candidates
    for name in ("gpt", "claude", "grok"):
        config.backends[name].enabled = False
    decision = route(config, "@yolo solve this")
    assert decision.capacity_blocked
    assert "no eligible provider" in (decision.capacity_detail or "")
    rows = decision.selection_receipt["provider_selection"]["candidates"]
    assert "azure" not in {row["backend"] for row in rows}


def test_pin_reaches_a_backend_outside_the_allowlist(config):
    decision = route(config, "@azure @yolo use my Astra deployment")
    assert not decision.capacity_blocked, decision.capacity_detail
    assert (decision.backend, decision.model, decision.reasoning_effort) == (
        "azure", "dev-gpt-6-astra", "ultra"
    )


def test_admitted_azure_is_last(config):
    config.routing.provider_selection.candidates = ["azure", "grok", "gpt", "claude"]
    for name in ("gpt", "claude", "grok"):
        config.backends[name].enabled = False
    decision = route(config, "@yolo go")
    assert decision.backend == "azure"
    priority = decision.selection_receipt["provider_selection"]["yolo_priority"]
    assert priority == ["azure"]


# --- 3. Bundled YOLO targets stay in memory ------------------------------------------


def four_tier_payload():
    payload = default_config().model_dump(mode="json", exclude_none=True)
    for backend in payload["backends"].values():
        backend["tiers"].pop("yolo", None)
    payload["providers"]["codex"]["tiers"].pop("yolo", None)
    return payload


def test_save_does_not_persist_bundled_yolo(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(four_tier_payload()), encoding="utf-8")
    config = load_config(path)
    assert config.backends["gpt"].tiers["yolo"].reasoning_effort == "ultra"
    ensure_claude_bridge_backend(config, 8090)
    ensure_grok_backend(config)
    save_config(config, path)
    assert "yolo" not in path.read_text(encoding="utf-8")
    assert load_config(path).backends["claude"].tiers["yolo"].model == "claude-fable-5-2"


def test_new_install_save_has_no_yolo(tmp_path):
    path = save_config(default_config(), tmp_path / "config.yaml")
    assert "yolo" not in path.read_text(encoding="utf-8")


def test_explicit_and_changed_yolo_entries_round_trip(tmp_path):
    payload = four_tier_payload()
    payload["backends"]["gpt"]["tiers"]["yolo"] = {
        "model": "gpt-6.2-astra", "reasoning_effort": "ultra"
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    config = load_config(path)
    save_config(config, path)
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["backends"]["gpt"]["tiers"]["yolo"]["model"] == "gpt-6.2-astra"
    # A changed bundled entry is the operator's choice and is persisted.
    loaded = load_config(path)
    ensure_grok_backend(loaded)
    loaded.backends["grok"].tiers["yolo"] = ModelTarget(model="grok-4.7", reasoning_effort="high")
    save_config(loaded, path)
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["backends"]["grok"]["tiers"]["yolo"]["reasoning_effort"] == "high"


def test_legacy_no_backends_config_does_not_leak_yolo_into_codex_provider(tmp_path):
    payload = four_tier_payload()
    payload.pop("backends")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    config = load_config(path)
    assert "yolo" in config.backends["gpt"].tiers
    assert "yolo" not in config.providers["codex"].tiers
    save_config(config, path)
    assert "yolo" not in path.read_text(encoding="utf-8")


# --- 4. GPT/Azure reuse the configured Astra MAX name ------------------------------


@pytest.mark.parametrize(
    ("name", "max_model", "expected"),
    [
        ("azure", "dev-gpt-6-astra", "dev-gpt-6-astra"),
        ("azure", "gpt-5", None),
        ("gpt", "gpt-6-astra", "gpt-6-astra"),
        ("gpt", "gpt-6.2-sol", None),
    ],
)
def test_astra_derivation(tmp_path, name, max_model, expected):
    payload = four_tier_payload()
    payload["backends"][name]["tiers"]["max"] = {"model": max_model, "reasoning_effort": "high"}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    yolo = load_config(path).backends[name].tiers.get("yolo")
    assert (yolo.model if yolo else None) == expected
    if yolo:
        assert yolo.reasoning_effort == "ultra"


def test_unmapped_azure_is_explicitly_unavailable(config):
    config.backends["azure"].tiers.pop("yolo")
    decision = route(config, "@azure @yolo go")
    assert decision.capacity_blocked
    assert "no_yolo_mapping" in (decision.capacity_detail or "")
