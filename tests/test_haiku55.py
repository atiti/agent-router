import json

import pytest

from agentroute.claude_bridge import (
    ResponsesStream,
    catalog_from_config,
    model_catalog,
    translate_request,
)
from agentroute.config import default_config
from agentroute.pricing import token_cost
from agentroute.providers import ensure_claude_bridge_backend


@pytest.mark.parametrize("choice", ["required", {"type": "function", "name": "read_file"}])
def test_haiku55_adaptive_effort_and_native_tool_choice(choice):
    body = {
        "model": "claude-haiku-5-5",
        "reasoning": {"effort": "low"},
        "input": [{"role": "user", "content": "read the file"}],
        "tools": [{"type": "function", "name": "read_file", "parameters": {}}],
        "tool_choice": choice,
    }
    payload, _, _ = translate_request(body)
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "low"}
    assert payload["max_tokens"] == 128_000
    assert payload["tool_choice"] == (
        {"type": "any"} if choice == "required" else {"type": "tool", "name": "read_file"}
    )
    body["max_output_tokens"] = 4096
    assert translate_request(body)[0]["max_tokens"] == 4096


@pytest.mark.parametrize("input_tokens,multiplier", [(100_000, 1), (100_001, 5)])
def test_haiku55_cost_threshold_includes_cached_prompt(input_tokens, multiplier):
    usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": 80_000,
        "cache_write_input_tokens": 10_000,
        "output_tokens": 2000,
    }
    expected = (input_tokens - 90_000) * 0.10 + 80_000 * 0.01 + 10_000 * 0.125 + 1000
    assert token_cost("claude-haiku-5-5", usage, default_config().pricing) == pytest.approx(
        expected * multiplier / 1_000_000
    )


def test_bridge_install_advertises_selected_haiku55_and_preserves_custom_mapping():
    config = default_config()
    ensure_claude_bridge_backend(config, 8090)
    descriptor = model_catalog(catalog_from_config(config))["models"][0]
    assert descriptor["slug"] == "claude-haiku-5-5"
    assert descriptor["context_window"] == 1_000_000
    assert descriptor["default_reasoning_level"] == "medium"
    assert config.backends["claude"].tiers["fast"].reasoning_effort == "low"
    config.backends["claude"].tiers["fast"].model = "custom-haiku"
    ensure_claude_bridge_backend(config, 8090)
    assert catalog_from_config(config)[0][0] == "custom-haiku"


@pytest.mark.parametrize(
    "stop_reason,event,status",
    [
        ("max_tokens", "response.incomplete", "incomplete"),
        ("model_context_window_exceeded", "response.incomplete", "incomplete"),
        ("refusal", "response.failed", "failed"),
    ],
)
def test_haiku55_stop_reasons_are_not_reported_as_completed(stop_reason, event, status):
    stream = ResponsesStream("resp_stop", "claude-haiku-5-5")
    stream.feed("message_delta", {"delta": {"stop_reason": stop_reason}})
    completed = [
        json.loads(chunk.split(b"data: ", 1)[1]) for chunk in stream.feed("message_stop", {})
    ]
    assert completed[-1]["type"] == event
    assert completed[-1]["response"]["status"] == status
    if event == "response.incomplete":
        assert completed[-1]["response"]["incomplete_details"] == {"reason": "max_output_tokens"}


def test_haiku55_catalog_reserves_context_for_128k_output():
    descriptor = model_catalog([("claude-haiku-5-5", 1_000_000)])["models"][0]
    assert descriptor["effective_context_window_percent"] == 85


def test_haiku55_thinking_replays_only_with_matching_profile_and_prefix():
    request = {
        "model": "claude-haiku-5-5",
        "instructions": "Be concise.",
        "tools": [],
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Use echo."}],
            }
        ],
    }
    stream = ResponsesStream(
        "resp_thought", request["model"], request_body=request, profile_scope="second"
    )
    stream.feed(
        "content_block_start",
        {"index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
    )
    stream.feed(
        "content_block_delta",
        {"index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
    )
    events = stream.feed("content_block_stop", {"index": 0})
    item = json.loads(events[-1].split(b"data: ", 1)[1])["item"]
    followup = {
        **request,
        "input": [
            *request["input"],
            item,
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Continue."}],
            },
        ],
    }

    payload, _, _ = translate_request(followup, mode="claude-code", profile_scope="second")
    assert payload["messages"][1]["content"] == [
        {"type": "thinking", "thinking": "", "signature": "sig"}
    ]

    foreign, _, _ = translate_request(followup, mode="claude-code", profile_scope="third")
    assert all(
        block.get("type") != "thinking"
        for message in foreign["messages"]
        for block in message["content"]
    )

    changed = {**followup, "instructions": "Changed."}
    mismatched, _, _ = translate_request(changed, mode="claude-code", profile_scope="second")
    assert all(
        block.get("type") != "thinking"
        for message in mismatched["messages"]
        for block in message["content"]
    )
