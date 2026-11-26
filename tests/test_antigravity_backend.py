"""Tests for the Antigravity subscription backend (agy stream-json bridge)."""

from __future__ import annotations

import json

import pytest

from agentroute import antigravity_bridge as bridge
from agentroute.antigravity_usage import parse_usage_output
from agentroute.config import default_config
from agentroute.pricing import canonical_model

USAGE_TABLE = (
    "Gemini Models\tWeekly Limit Remaining\t88%\t2026-12-03T07:15:35Z\n"
    "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-12-03T06:55:01Z\n"
)


def body(prompt: str = "Say hello", **extra):
    payload = {
        "model": "gemini-3.8-flash-medium",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
    }
    payload.update(extra)
    return payload


def test_parse_usage_reads_groups():
    groups = parse_usage_output(USAGE_TABLE)
    assert [group["name"] for group in groups] == ["Gemini Models", "Claude and GPT models"]
    assert groups[0]["remaining_percent"] == 88.0
    assert groups[0]["resets_at"] == "2026-12-03T07:15:35Z"
    assert parse_usage_output("garbage") == []


def test_latest_prompt_takes_the_newest_user_turn():
    payload = {
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "first"}]},
            {"role": "assistant", "content": [{"type": "output_text", "text": "reply"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "second"}]},
        ]
    }
    assert bridge.latest_prompt(payload) == "second"
    assert bridge.latest_prompt({"input": "plain"}) == "plain"


def test_latest_prompt_raises_without_a_user_turn():
    with pytest.raises(bridge.AntigravityError, match="no user message"):
        bridge.latest_prompt({"input": []})


def test_thread_key_is_stable_for_the_same_opening_turn():
    first = bridge.thread_key(body("same opening"))
    second = bridge.thread_key(body("same opening"))
    assert first == second
    assert first != bridge.thread_key(body("different"))
    assert bridge.thread_key(body("x", prompt_cache_key="k")) == "cache:k"


def test_sessions_persist_and_bound_themselves(tmp_path):
    path = tmp_path / "sessions.json"
    sessions = bridge.Sessions(path=path, limit=2)
    sessions.put("a", "1")
    sessions.put("b", "2")
    sessions.put("c", "3")
    reopened = bridge.Sessions(path=path)
    assert reopened.get("a") is None
    assert reopened.get("c") == "3"


def test_effort_comes_from_the_model_id_when_it_carries_one():
    assert bridge.effort_for("gemini-3.8-flash-low", None) == "low"
    assert bridge.effort_for("gemini-4-hydrogen-high", None) == "high"
    assert bridge.effort_for("gemini-3.8-flash", "medium") == "medium"
    assert bridge.effort_for("gemini-3.8-flash", "bogus") is None


def test_command_passes_stream_json_and_resume(monkeypatch):
    monkeypatch.setenv("AGENTROUTE_ANTIGRAVITY_BINARY", "/opt/bin/agy")
    monkeypatch.delenv("AGENTROUTE_ANTIGRAVITY_MODE", raising=False)
    argv = bridge.command(
        prompt="hi", model="gemini-3.8-flash-medium", effort="medium", conversation="cid"
    )
    assert argv[:3] == ["/opt/bin/agy", "-p", "hi"]
    assert "--output-format" in argv and "stream-json" in argv
    assert "--model" in argv and "gemini-3.8-flash-medium" in argv
    assert "--effort" in argv and "medium" in argv
    assert "--conversation" in argv and "cid" in argv


def test_usage_maps_thinking_into_output_and_cache_into_cached():
    usage = bridge.usage_from(
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "thinking_tokens": 5,
            "cache_read_tokens": 40,
            "total_tokens": 125,
        }
    )
    assert usage == {
        "input_tokens": 100,
        "output_tokens": 25,
        "total_tokens": 125,
        "cached_input_tokens": 40,
    }


def test_stream_translates_text_deltas_and_usage():
    turn = bridge.Turn("gemini-3.8-flash-medium")
    events = [
        json.loads(frame.split("data: ", 1)[1])
        for frame in iter_stream(turn, stream_events())
        if frame.startswith("event:")
    ]
    names = [event["type"] for event in events]
    assert "response.output_text.delta" in names
    completed = [e for e in events if e["type"] == "response.completed"][0]["response"]
    assert completed["output"][0]["content"][0]["text"] == "Hello there"
    assert completed["usage"]["input_tokens"] == 5081
    assert completed["usage"]["output_tokens"] == 40


def test_tool_steps_are_recorded_not_forwarded_as_codex_tools():
    turn = bridge.Turn("gemini-3.8-flash-medium")
    list(
        turn.feed(
            {
                "event": "step_update",
                "step_update": {"step_type": "tool", "tool_name": "run_command", "state": "ACTIVE"},
            }
        )
    )
    list(turn.complete())
    assert turn.tools == ["run_command"]


def test_result_text_is_used_when_no_delta_arrived():
    turn = bridge.Turn("gemini-3.8-flash-medium")
    list(
        turn.feed(
            {
                "event": "result",
                "result": {"status": "SUCCESS", "response": "final", "conversation_id": "cid"},
            }
        )
    )
    assert turn.conversation == "cid"
    assert turn.status == "SUCCESS"


def test_backend_registration_declares_text_only_and_prices_models():
    from agentroute.antigravity_cli import ensure_backend

    config = default_config()
    assert ensure_backend(config, 8816) is True
    backend = config.backends["antigravity"]
    assert backend.codex_provider == "agentroute-antigravity"
    assert backend.base_url == "http://127.0.0.1:8816/v1"
    assert backend.tiers["normal"].model == bridge.MODELS["normal"]
    for model in bridge.MODELS.values():
        assert config.capabilities.models[model].tool_calling == "none"
        assert canonical_model(model, config.pricing) in config.pricing.models
    # Tier defaults stay where the operator put them.
    assert config.routing.backend_by_tier["normal"] == "gpt"


def test_backend_registration_is_idempotent():
    from agentroute.antigravity_cli import ensure_backend

    config = default_config()
    ensure_backend(config, 8816)
    ensure_backend(config, 8817)
    assert config.backends["antigravity"].base_url == "http://127.0.0.1:8817/v1"


def test_missing_cli_is_reported_clearly(monkeypatch):
    monkeypatch.setenv("AGENTROUTE_ANTIGRAVITY_BINARY", "definitely-not-installed-xyz")
    with pytest.raises(bridge.AntigravityError, match="not found"):
        bridge.run_turn("hi", "gemini-3.8-flash-medium", None, None)


def iter_stream(turn, events):
    frames = []
    for event in events:
        frames.extend(frame.decode() for frame in turn.feed(event))
    frames.extend(frame.decode() for frame in turn.complete())
    return frames


def stream_events():
    return [
        {"event": "init", "init": {"conversation_id": "cid"}},
        {
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "state": "ACTIVE",
                "text_delta": "Hello ",
            },
        },
        {
            "event": "step_update",
            "step_update": {"step_type": "agent_response", "state": "DONE", "text_delta": "there"},
        },
        {
            "event": "result",
            "result": {
                "conversation_id": "cid",
                "status": "SUCCESS",
                "response": "Hello there",
                "usage": {
                    "input_tokens": 5081,
                    "output_tokens": 40,
                    "thinking_tokens": 0,
                    "cache_read_tokens": 3438,
                    "total_tokens": 5121,
                },
            },
        },
    ]


def test_capacity_reports_the_subscription_allowance(monkeypatch):
    from agentroute import capacity
    from agentroute.antigravity_usage import parse_usage_output

    monkeypatch.setattr(
        "agentroute.antigravity_usage.fetch_limits",
        lambda **kwargs: {"groups": parse_usage_output(USAGE_TABLE)},
    )
    config = default_config()
    config.backends["antigravity"] = config.backends["gpt"].model_copy(deep=True)
    state = capacity.backend_state(config, "antigravity", probe=True)
    assert state.status == "healthy"
    assert "88% remaining" in state.detail
    assert state.used_percent == pytest.approx(12.0)
    assert state.resets_at is not None


def test_capacity_marks_an_exhausted_allowance(monkeypatch):
    from agentroute import capacity

    monkeypatch.setattr(
        "agentroute.antigravity_usage.fetch_limits",
        lambda **kwargs: {
            "groups": [{"name": "Gemini Models", "remaining_percent": 0.0, "resets_at": None}]
        },
    )
    config = default_config()
    config.backends["antigravity"] = config.backends["gpt"].model_copy(deep=True)
    state = capacity.backend_state(config, "antigravity", probe=True)
    assert state.status == "exhausted"
    assert not state.available


def test_service_definition_pins_the_resolved_binary(monkeypatch):
    """launchd's PATH excludes ~/.local/bin, so the path must be absolute."""
    import agentroute.antigravity_cli as cli

    monkeypatch.setenv("AGENTROUTE_ANTIGRAVITY_BINARY", "/wrong/from/shell/agy")
    monkeypatch.setenv("AGENTROUTE_ANTIGRAVITY_CWD", "/srv/repo")
    definition = cli.service_definition(8816, "/Users/me/.local/bin/agy")
    env = definition["EnvironmentVariables"]
    assert env["AGENTROUTE_ANTIGRAVITY_BINARY"] == "/Users/me/.local/bin/agy"
    assert env["AGENTROUTE_ANTIGRAVITY_CWD"] == "/srv/repo"


def test_systemd_unit_pins_the_resolved_binary(monkeypatch):
    import agentroute.antigravity_cli as cli

    monkeypatch.delenv("AGENTROUTE_ANTIGRAVITY_BINARY", raising=False)
    text = cli.systemd_unit_text(8816, "/opt/agy")
    assert "Environment=AGENTROUTE_ANTIGRAVITY_BINARY=/opt/agy" in text


def test_bridge_uses_the_pinned_binary(monkeypatch):
    monkeypatch.setenv("AGENTROUTE_ANTIGRAVITY_BINARY", "/opt/pinned/agy")
    assert bridge.binary() == "/opt/pinned/agy"
