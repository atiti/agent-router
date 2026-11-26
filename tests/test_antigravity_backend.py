"""Tests for the Antigravity backend used as a model behind Codex's harness."""

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

SHELL_TOOL = {
    "type": "function",
    "name": "shell",
    "description": "Run a shell command.",
    "parameters": {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "Command line"}},
        "required": ["command"],
    },
}

PATCH_TOOL = {"type": "custom", "name": "apply_patch", "description": "Apply a patch."}


def body(prompt: str = "List files", **extra):
    payload = {
        "model": "gemini-3.8-flash-medium",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
    }
    payload.update(extra)
    return payload


def frames(item):
    handler = object.__new__(bridge.AntigravityHandler)
    response = {"id": "r", "model": "m"}
    return [f.decode() for f in handler._stream(item, response)]


def data_of(chunks, event):
    block = [c for c in chunks if c.startswith(f"event: {event}")][-1]
    return json.loads(block.split("data: ", 1)[1])


def test_tool_definitions_render_function_and_freeform_tools():
    lines, kinds = bridge.tool_definitions({"tools": [SHELL_TOOL, PATCH_TOOL]})
    assert kinds == {"shell": "function", "apply_patch": "custom"}
    assert "shell: arguments {command:" in lines[0]
    assert "Command line" in lines[0]
    assert 'apply_patch: freeform text tool. arguments {"input": string}' in lines[1]


def test_build_prompt_carries_instructions_tools_and_history():
    payload = body(
        "Fix the bug",
        instructions="You are Codex.",
        tools=[SHELL_TOOL],
    )
    payload["input"].append(
        {
            "type": "function_call",
            "name": "shell",
            "arguments": '{"command":"ls"}',
            "call_id": "c1",
        }
    )
    payload["input"].append({"type": "function_call_output", "call_id": "c1", "output": "a.txt"})
    prompt = bridge.build_prompt(payload)
    assert "You are Codex." in prompt
    assert "TOOL DEFINITIONS" in prompt and "shell" in prompt
    assert "USER:\nFix the bug" in prompt
    assert "ASSISTANT CALLED shell" in prompt
    assert "RESULT OF shell:\na.txt" in prompt
    assert prompt.rstrip().endswith("REPLY:")


def test_protocol_forbids_running_tools_itself():
    assert "never run anything yourself" in bridge.PROTOCOL
    assert "at most one tool" in bridge.PROTOCOL


def test_parse_reply_accepts_plain_text():
    assert bridge.parse_reply("  All done.  ") == ("text", "  All done.  ", None)


def test_parse_reply_reads_a_json_tool_call():
    kind, name, arguments = bridge.parse_reply('{"tool":"shell","arguments":{"command":"ls"}}')
    assert (kind, name) == ("tool", "shell")
    assert arguments == {"command": "ls"}


def test_parse_reply_tolerates_a_code_fence():
    text = 'Here you go:\n```json\n{"tool":"shell","arguments":{"command":"pwd"}}\n```'
    kind, name, arguments = bridge.parse_reply(text)
    assert (kind, name, arguments) == ("tool", "shell", {"command": "pwd"})


def test_parse_reply_treats_json_without_a_tool_key_as_prose():
    kind, value, _ = bridge.parse_reply('{"answer":42}')
    assert kind == "text"
    assert value == '{"answer":42}'


def test_command_is_plan_mode_and_stateless(monkeypatch):
    monkeypatch.setenv("AGENTROUTE_ANTIGRAVITY_BINARY", "/opt/bin/agy")
    argv = bridge.command("prompt", "gemini-3.8-flash-medium", "medium")
    assert argv[:3] == ["/opt/bin/agy", "-p", "prompt"]
    assert "--mode" in argv and "plan" in argv
    assert "--output-format" in argv and "stream-json" in argv
    assert "--effort" in argv and "medium" in argv
    # Codex owns the conversation; no agy-side resume.
    assert "--conversation" not in argv


def test_effort_comes_from_the_model_id():
    assert bridge.effort_for("gemini-3.8-flash-low", None) == "low"
    assert bridge.effort_for("gemini-3.8-flash", "high") == "high"
    assert bridge.effort_for("gemini-3.8-flash", "bogus") is None


def test_usage_maps_thinking_and_cache():
    assert bridge.usage_from(
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "thinking_tokens": 5,
            "cache_read_tokens": 40,
            "total_tokens": 125,
        }
    ) == {
        "input_tokens": 100,
        "output_tokens": 25,
        "total_tokens": 125,
        "cached_input_tokens": 40,
    }


def test_function_reply_becomes_a_codex_function_call():
    item = bridge.AntigravityHandler._item(
        "tool", "shell", {"command": "ls"}, {"shell": "function"}
    )
    chunks = frames(item)
    assert item["type"] == "function_call"
    assert json.loads(item["arguments"]) == {"command": "ls"}
    completed = data_of(chunks, "response.completed")
    assert completed["response"]["output"][0]["type"] == "function_call"
    assert "response.function_call_arguments.done" in "".join(chunks)


def test_freeform_tool_reply_becomes_a_custom_tool_call():
    item = bridge.AntigravityHandler._item(
        "tool", "apply_patch", {"input": "*** Begin Patch"}, {"apply_patch": "custom"}
    )
    chunks = frames(item)
    assert item["type"] == "custom_tool_call"
    assert item["input"] == "*** Begin Patch"
    assert "response.custom_tool_call_input.done" in "".join(chunks)


def test_text_reply_becomes_a_message():
    item = bridge.AntigravityHandler._item("text", "Hello", None, {})
    chunks = frames(item)
    assert item["type"] == "message"
    assert data_of(chunks, "response.output_text.delta")["delta"] == "Hello"


def test_unknown_tool_name_still_returns_a_function_call():
    """The harness decides whether an undeclared name is acceptable; we do not drop it."""
    item = bridge.AntigravityHandler._item("tool", "mystery", {}, {})
    assert item["type"] == "function_call"
    assert item["name"] == "mystery"


def test_missing_cli_is_reported_clearly(monkeypatch):
    monkeypatch.setenv("AGENTROUTE_ANTIGRAVITY_BINARY", "definitely-not-installed-xyz")
    with pytest.raises(bridge.AntigravityError, match="not found"):
        bridge.run_turn("prompt", "gemini-3.8-flash-medium", None)


def test_backend_registration_declares_full_tools_but_no_parallel_calls():
    from agentroute.antigravity_cli import ensure_backend

    config = default_config()
    assert ensure_backend(config, 8816) is True
    backend = config.backends["antigravity"]
    assert backend.codex_provider == "agentroute-antigravity"
    assert backend.tool_compatibility == "functions_and_apply_patch_preserve_reasoning"
    for model in bridge.MODELS.values():
        capabilities = config.capabilities.models[model]
        assert capabilities.tool_calling == "full"
        assert capabilities.parallel_tool_calls is False
        assert canonical_model(model, config.pricing) in config.pricing.models
    assert config.routing.backend_by_tier["normal"] == "gpt"


def test_service_definition_pins_the_resolved_binary(monkeypatch):
    """launchd's PATH excludes ~/.local/bin, so the path must be absolute."""
    import agentroute.antigravity_cli as cli

    monkeypatch.setenv("AGENTROUTE_ANTIGRAVITY_BINARY", "/wrong/from/shell/agy")
    definition = cli.service_definition(8816, "/Users/me/.local/bin/agy")
    assert definition["EnvironmentVariables"]["AGENTROUTE_ANTIGRAVITY_BINARY"] == (
        "/Users/me/.local/bin/agy"
    )


def test_parse_usage_reads_groups():
    groups = parse_usage_output(USAGE_TABLE)
    assert [group["name"] for group in groups] == ["Gemini Models", "Claude and GPT models"]
    assert groups[0]["remaining_percent"] == 88.0


def test_capacity_reports_the_subscription_allowance(monkeypatch):
    from agentroute import capacity

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
