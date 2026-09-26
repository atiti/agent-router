import json
import threading
import urllib.error
import urllib.request

import pytest

from agentroute.claude_bridge import (
    API_KEY_BETAS,
    CLAUDE_CODE_BETAS,
    DEFAULT_MODEL,
    LONG_CONTEXT_BETA,
    ApiKeyCredential,
    BridgeHandler,
    BridgeServer,
    CredentialError,
    ResponsesStream,
    anthropic_headers,
    anthropic_tool_name,
    catalog_from_config,
    collect_tools,
    model_catalog,
    resolve_credential,
    translate_request,
)
from agentroute.config import ExecutionBackendConfig, ModelTarget, default_config


def test_messages_and_instructions_translate_for_api_key_mode():
    payload, freeform, renames = translate_request(
        {
            "model": "claude-sonnet-5",
            "instructions": "Be terse.",
            "input": [
                {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": "ignored system role"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            ],
        },
        mode="api-key",
    )

    assert payload["model"] == "claude-sonnet-5"
    assert payload["stream"] is True
    # An API key request must not impersonate Claude Code.
    assert payload["system"] == [{"type": "text", "text": "Be terse."}]
    assert [m["role"] for m in payload["messages"]] == ["user", "user"]
    assert payload["messages"][0]["content"] == [{"type": "text", "text": "ignored system role"}]
    assert freeform == set()
    assert renames == {}


def test_subscription_mode_prepends_claude_code_identity():
    payload, _, _ = translate_request(
        {
            "model": DEFAULT_MODEL,
            "instructions": "Be terse.",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                }
            ],
        },
        mode="claude-code",
    )

    assert len(payload["system"]) == 3
    assert payload["system"][0]["text"].startswith("x-anthropic-billing-header: cc_version=")
    assert payload["system"][1]["text"].startswith("You are Claude Code,")
    assert payload["system"][2] == {"type": "text", "text": "Be terse."}


def test_tool_calls_outputs_and_reasoning_history_translate():
    payload, _, _ = translate_request(
        {
            "model": DEFAULT_MODEL,
            "input": [
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "think"}]},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "get_weather",
                    "arguments": '{"city": "Budapest"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "18C",
                },
                {"type": "reasoning", "summary": []},
            ],
        },
        mode="api-key",
    )

    # Reasoning history is dropped: Anthropic rejects unsigned thinking blocks.
    assert [m["role"] for m in payload["messages"]] == ["assistant", "user"]
    assert payload["messages"][0]["content"] == [
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "get_weather",
            "input": {"city": "Budapest"},
        }
    ]
    assert payload["messages"][1]["content"] == [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "18C"}
    ]


def test_parallel_tool_outputs_are_grouped_into_one_user_message():
    payload, _, _ = translate_request(
        {
            "model": DEFAULT_MODEL,
            "input": [
                {"type": "function_call", "call_id": "a", "name": "one", "arguments": "{}"},
                {"type": "function_call", "call_id": "b", "name": "two", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "a", "output": "1"},
                {"type": "function_call_output", "call_id": "b", "output": "2"},
            ],
        },
        mode="api-key",
    )

    assert [m["role"] for m in payload["messages"]] == ["assistant", "assistant", "user"]
    assert [block["tool_use_id"] for block in payload["messages"][-1]["content"]] == ["a", "b"]


def test_custom_tool_becomes_string_parameter_and_is_tracked():
    tools, freeform, renames = collect_tools(
        [
            {
                "type": "custom",
                "name": "apply_patch",
                "description": "Edit files.",
                "format": {"type": "grammar", "syntax": "lark", "definition": "start: patch"},
            },
            {
                "type": "function",
                "name": "weird.name",
                "description": "Dotted name.",
                "parameters": {"type": "object", "properties": {}},
            },
            {"type": "web_search"},
            {"type": "tool_search", "execution": "client"},
        ]
    )

    assert freeform == {"apply_patch"}
    assert [tool["name"] for tool in tools] == ["apply_patch", "weird_name"]
    assert renames == {"weird_name": "weird.name"}
    patch_tool = tools[0]
    assert patch_tool["input_schema"]["required"] == ["input"]
    assert "start: patch" in patch_tool["description"]
    # Unsupported hosted tools are dropped rather than mis-declared.
    assert len(tools) == 2


def test_duplicate_tool_names_are_declared_once():
    tools, freeform, _ = collect_tools(
        [
            {"type": "function", "name": "shell", "parameters": {"type": "object"}},
            {"type": "function", "name": "shell", "parameters": {"type": "object"}},
            {
                "type": "namespace",
                "name": "outer",
                "tools": [
                    {
                        "type": "custom",
                        "name": "apply_patch",
                        "description": "Edit files.",
                        "format": {
                            "type": "grammar",
                            "syntax": "lark",
                            "definition": "start: patch",
                        },
                    }
                ],
            },
            {
                "type": "custom",
                "name": "apply_patch",
                "description": "Duplicate declaration.",
                "format": {"type": "grammar", "syntax": "lark", "definition": "start: other"},
            },
        ]
    )

    # Anthropic rejects any repeated tool name, so the first declaration wins.
    assert [tool["name"] for tool in tools] == ["shell", "apply_patch"]
    assert freeform == {"apply_patch"}
    assert "start: patch" in tools[1]["description"]


def test_tool_choice_maps_to_anthropic_modes():
    base = {
        "model": DEFAULT_MODEL,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            }
        ],
        "tools": [
            {
                "type": "function",
                "name": "t",
                "description": "",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
    }
    assert translate_request({**base, "tool_choice": "none"})[0]["tool_choice"] == {"type": "none"}
    assert translate_request({**base, "tool_choice": "required"})[0]["tool_choice"] == {
        "type": "any"
    }
    assert translate_request({**base, "tool_choice": "auto"})[0]["tool_choice"] == {"type": "auto"}
    assert "tools" not in translate_request({**base, "tools": []})[0]


def test_tool_name_sanitizing_respects_anthropic_limits():
    assert anthropic_tool_name("weird.name") == "weird_name"
    assert anthropic_tool_name("nested/tool:name") == "nested_tool_name"
    assert len(anthropic_tool_name("x" * 80)) == 64
    assert anthropic_tool_name("") == "tool"


def test_long_context_beta_is_omitted_for_models_that_reject_it():
    sonnet = anthropic_headers("token", "claude-sonnet-5", "claude-code")["anthropic-beta"]
    haiku = anthropic_headers("token", "claude-haiku-4-5-20251001", "claude-code")["anthropic-beta"]

    assert LONG_CONTEXT_BETA in sonnet
    assert LONG_CONTEXT_BETA not in haiku
    assert "oauth-2025-04-20" in sonnet
    assert all(beta in sonnet for beta in CLAUDE_CODE_BETAS)


def test_api_key_mode_uses_api_key_header_and_plain_betas():
    headers = anthropic_headers("secret-key", "claude-sonnet-5", "api-key")

    assert headers["x-api-key"] == "secret-key"
    assert "authorization" not in headers
    assert headers["anthropic-beta"] == ",".join(API_KEY_BETAS)
    assert "You are Claude Code" not in json.dumps(headers)


def test_api_key_credential_rejects_empty_values():
    with pytest.raises(CredentialError):
        ApiKeyCredential("")


def test_resolve_credential_prefers_api_key_and_honours_explicit_modes(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert resolve_credential("claude-code").mode == "claude-code"
    assert resolve_credential("auto").mode == "claude-code"
    assert resolve_credential("api-key", api_key="abc").mode == "api-key"
    assert resolve_credential("auto", api_key="abc").mode == "api-key"
    with pytest.raises(CredentialError):
        resolve_credential("banana")


def test_catalog_follows_backend_tiers_without_duplicates():
    config = default_config()
    config.backends["claude"] = ExecutionBackendConfig(
        enabled=True,
        codex_provider="agentroute-claude",
        display_name="Claude",
        base_url="http://127.0.0.1:8090/v1",
        tiers={
            "fast": ModelTarget(model="claude-haiku-4-5-20251001"),
            "normal": ModelTarget(model="claude-sonnet-5"),
            "smart": ModelTarget(model="claude-sonnet-5"),
            "max": ModelTarget(model="claude-opus-5-5"),
        },
    )

    assert catalog_from_config(config) == [
        ("claude-haiku-4-5-20251001", 200_000),
        ("claude-sonnet-5", 200_000),
        ("claude-opus-5-5", 200_000),
    ]


def test_model_catalog_exposes_codex_descriptor_fields():
    payload = model_catalog([("claude-sonnet-5", 200_000)])

    assert payload["object"] == "list"
    assert payload["data"] == [{"id": "claude-sonnet-5"}]
    descriptor = payload["models"][0]
    assert descriptor["slug"] == "claude-sonnet-5"
    assert descriptor["apply_patch_tool_type"] == "freeform"
    assert descriptor["shell_type"] == "unified_exec"
    assert descriptor["context_window"] == 200_000


def test_stream_emits_text_deltas_then_completed():
    stream = ResponsesStream("resp_1", "claude-sonnet-5")
    events = []
    events += stream.feed("message_start", {"message": {"usage": {"input_tokens": 11}}})
    events += stream.feed("content_block_start", {"index": 0, "content_block": {"type": "text"}})
    events += stream.feed(
        "content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "He"}}
    )
    events += stream.feed(
        "content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "llo"}}
    )
    events += stream.feed("message_delta", {"usage": {"output_tokens": 2}})
    events += stream.feed("message_stop", {})

    kinds = [json.loads(chunk.decode().split("data: ", 1)[1])["type"] for chunk in events]
    assert kinds == [
        "response.created",
        "response.output_item.added",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_item.done",
        "response.completed",
    ]
    # Codex panics on a text delta that arrives before its item is announced.
    announced = json.loads(events[1].decode().split("data: ", 1)[1])
    assert announced["item"] == {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "content": [{"type": "output_text", "text": ""}],
    }
    deltas = [
        json.loads(chunk.decode().split("data: ", 1)[1])["delta"]
        for chunk in events
        if b"output_text.delta" in chunk
    ]
    assert deltas == ["He", "llo"]
    completed = json.loads(events[-1].decode().split("data: ", 1)[1])
    assert completed["response"]["usage"]["input_tokens"] == 11
    assert completed["response"]["usage"]["total_tokens"] == 13
    assert completed["response"]["output"][0]["content"] == [
        {"type": "output_text", "text": "Hello"}
    ]


def test_stream_skips_empty_text_blocks():
    stream = ResponsesStream("resp_empty", "claude-sonnet-5")
    events = []
    events += stream.feed("message_start", {"message": {"usage": {"input_tokens": 1}}})
    events += stream.feed("content_block_start", {"index": 0, "content_block": {"type": "text"}})
    events += stream.feed("content_block_stop", {"index": 0})
    events += stream.feed("message_stop", {})

    kinds = [json.loads(chunk.decode().split("data: ", 1)[1])["type"] for chunk in events]
    # An empty assistant message would otherwise be replayed into the transcript.
    assert kinds == ["response.created", "response.completed"]


def test_stream_maps_function_calls_to_responses_items():
    stream = ResponsesStream("resp_2", "claude-sonnet-5")
    events = []
    events += stream.feed(
        "content_block_start",
        {"index": 0, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_weather"}},
    )
    events += stream.feed(
        "content_block_delta",
        {"index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"city":'}},
    )
    events += stream.feed(
        "content_block_delta",
        {"index": 0, "delta": {"type": "input_json_delta", "partial_json": '"BP"}'}},
    )
    events += stream.feed("content_block_stop", {"index": 0})
    events += stream.feed("message_stop", {})

    added = json.loads(events[0].decode().split("data: ", 1)[1])
    assert added["type"] == "response.output_item.added"
    assert added["item"] == {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "toolu_1",
        "name": "get_weather",
        "arguments": '{"city": "BP"}',
    }


def test_stream_emits_freeform_custom_tool_input():
    stream = ResponsesStream("resp_3", "claude-sonnet-5", freeform={"apply_patch"})
    events = []
    events += stream.feed(
        "content_block_start",
        {"index": 0, "content_block": {"type": "tool_use", "id": "toolu_2", "name": "apply_patch"}},
    )
    events += stream.feed(
        "content_block_delta",
        {
            "index": 0,
            "delta": {
                "type": "input_json_delta",
                "partial_json": json.dumps({"input": "*** Begin Patch\n*** End Patch"}),
            },
        },
    )
    events += stream.feed("content_block_stop", {"index": 0})

    delta = json.loads(events[0].decode().split("data: ", 1)[1])
    assert delta["type"] == "response.custom_tool_call_input.delta"
    assert delta["delta"] == "*** Begin Patch\n*** End Patch"
    item = json.loads(events[1].decode().split("data: ", 1)[1])["item"]
    assert item["type"] == "custom_tool_call"
    assert item["name"] == "apply_patch"
    assert item["input"] == "*** Begin Patch\n*** End Patch"


def test_stream_restores_renamed_tool_names():
    stream = ResponsesStream("resp_4", "claude-sonnet-5", renames={"weird_name": "weird.name"})
    events = []
    events += stream.feed(
        "content_block_start",
        {"index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "weird_name"}},
    )
    events += stream.feed(
        "content_block_delta",
        {"index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
    )
    events += stream.feed("content_block_stop", {"index": 0})

    item = json.loads(events[0].decode().split("data: ", 1)[1])["item"]
    assert item["name"] == "weird.name"


def test_stream_reports_upstream_errors_as_response_failed():
    stream = ResponsesStream("resp_5", "claude-sonnet-5")
    events = stream.feed("error", {"error": {"type": "rate_limit_error", "message": "slow down"}})

    failed = json.loads(events[0].decode().split("data: ", 1)[1])
    assert failed["type"] == "response.failed"
    assert failed["response"]["error"] == {
        "code": "rate_limit_error",
        "message": "slow down",
    }


class _FakeCredential:
    mode = "api-key"

    def token(self):
        return "fake-key"

    def invalidate(self):
        return


@pytest.fixture
def bridge_server():
    """A live bridge with a stubbed Anthropic stream, to exercise the HTTP path."""
    previous = {
        "stream_factory": BridgeHandler.stream_factory,
        "credentials": getattr(BridgeHandler, "credentials", None),
        "models": BridgeHandler.models,
        "default_model": BridgeHandler.default_model,
    }
    captured = {}

    def fake_stream(credentials, payload):
        captured["credentials"] = credentials
        captured["payload"] = payload
        yield "message_start", {"message": {"usage": {"input_tokens": 5}}}
        yield "content_block_start", {"index": 0, "content_block": {"type": "text"}}
        yield (
            "content_block_delta",
            {
                "index": 0,
                "delta": {"type": "text_delta", "text": "ok"},
            },
        )
        yield "message_delta", {"usage": {"output_tokens": 1}}
        yield "message_stop", {}

    BridgeHandler.stream_factory = staticmethod(fake_stream)
    BridgeHandler.credentials = _FakeCredential()
    BridgeHandler.models = [("claude-sonnet-5", 200_000)]
    BridgeHandler.default_model = "claude-sonnet-5"
    server = BridgeServer(("127.0.0.1", 0), BridgeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, captured
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        BridgeHandler.stream_factory = previous["stream_factory"]
        if previous["credentials"] is not None:
            BridgeHandler.credentials = previous["credentials"]
        BridgeHandler.models = previous["models"]
        BridgeHandler.default_model = previous["default_model"]


def test_http_bridge_streams_responses_events(bridge_server):
    server, captured = bridge_server
    host, port = server.server_address[:2]
    body = json.dumps(
        {
            "model": "claude-sonnet-5",
            "instructions": "Be terse.",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi"}],
                }
            ],
            "tools": [],
        }
    ).encode()
    request = urllib.request.Request(
        f"http://{host}:{port}/v1/responses",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read().decode()

    assert response.status == 200
    assert "event: response.created" in payload
    assert '"delta": "ok"' in payload
    assert "event: response.completed" in payload
    # The handler must call the stream factory with exactly (credentials, payload);
    # a bound-method call would have passed the handler as a third argument.
    assert captured["payload"]["model"] == "claude-sonnet-5"
    assert captured["payload"]["system"] == [{"type": "text", "text": "Be terse."}]
    assert captured["credentials"].mode == "api-key"


def test_http_bridge_serves_models_and_health(bridge_server):
    server, _ = bridge_server
    host, port = server.server_address[:2]

    with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=30) as response:
        assert json.loads(response.read()) == {"status": "ok"}

    with urllib.request.urlopen(f"http://{host}:{port}/v1/models", timeout=30) as response:
        catalog = json.loads(response.read())
    assert catalog["data"] == [{"id": "claude-sonnet-5"}]
    assert catalog["models"][0]["slug"] == "claude-sonnet-5"


def test_http_bridge_reports_translation_and_routing_errors(bridge_server):
    server, _ = bridge_server
    host, port = server.server_address[:2]

    with pytest.raises(urllib.error.HTTPError) as unknown_path:
        urllib.request.urlopen(
            urllib.request.Request(
                f"http://{host}:{port}/v1/chat/completions",
                data=b"{}",
                headers={"content-type": "application/json"},
                method="POST",
            ),
            timeout=30,
        )
    assert unknown_path.value.code == 404

    with pytest.raises(urllib.error.HTTPError) as bad_json:
        urllib.request.urlopen(
            urllib.request.Request(
                f"http://{host}:{port}/v1/responses",
                data=b"{not json",
                headers={"content-type": "application/json"},
                method="POST",
            ),
            timeout=30,
        )
    assert bad_json.value.code == 400
