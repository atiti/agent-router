import base64
import copy
import hashlib
import io
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from agentroute.claude_bridge import (
    API_KEY_BETAS,
    CLAUDE_CODE_BETAS,
    CLAUDE_CODE_USER_AGENT,
    CREDENTIAL_EXPIRY_SKEW_MS,
    DEFAULT_MODEL,
    LONG_CONTEXT_BETA,
    RATE_LIMIT_EVENT,
    REFRESH_FAILURE_COOLDOWN_SECONDS,
    ApiKeyCredential,
    BridgeHandler,
    BridgeServer,
    ClaudeCodeCredential,
    CredentialError,
    ResponsesStream,
    UpstreamRateLimitError,
    anthropic_headers,
    anthropic_stream,
    anthropic_tool_name,
    catalog_from_config,
    codex_rate_limit_headers,
    collect_tools,
    describe_usage,
    fetch_subscription_usage,
    model_catalog,
    neutralize_codex_identity,
    parse_unified_rate_limits,
    read_usage_state,
    record_usage_snapshot,
    resolve_credential,
    summarize_subscription_usage,
    translate_request,
)
from agentroute.claude_profile_pool import ClaudeProfilePool
from agentroute.claude_profiles import profile_scope, profile_usage_path
from agentroute.config import (
    ClaudeSubscriptionProfile,
    ExecutionBackendConfig,
    ModelTarget,
    default_config,
)


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

    assert [m["role"] for m in payload["messages"]] == ["assistant", "user"]
    assert [block["id"] for block in payload["messages"][0]["content"]] == ["a", "b"]
    assert [block["tool_use_id"] for block in payload["messages"][-1]["content"]] == ["a", "b"]


def test_interrupted_parallel_tool_calls_receive_missing_results_before_next_turn():
    payload, _, _ = translate_request(
        {
            "model": DEFAULT_MODEL,
            "input": [
                {"type": "function_call", "call_id": "a", "name": "one", "arguments": "{}"},
                {"type": "function_call", "call_id": "b", "name": "two", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "a", "output": "1"},
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "continue"}],
                },
            ],
        },
        mode="api-key",
    )

    assert [message["role"] for message in payload["messages"]] == ["assistant", "user"]
    results = [
        block for block in payload["messages"][1]["content"] if block["type"] == "tool_result"
    ]
    assert {block["tool_use_id"] for block in results} == {"a", "b"}
    assert next(block for block in results if block["tool_use_id"] == "b")["is_error"] is True


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
        "model": "claude-haiku-4-5-20251001",
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


def _keychain_payload(*, expires_in_ms: int, access: str = "old-access"):
    return {
        "claudeAiOauth": {
            "accessToken": access,
            "refreshToken": "old-refresh",
            "expiresAt": int(time.time() * 1000) + expires_in_ms,
        }
    }


class _FakeOAuthResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeOAuthResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://platform.claude.com/v1/oauth/token", code, "error", None, io.BytesIO(body)
    )


def _credential(monkeypatch, payload, urlopen):
    credential = ClaudeCodeCredential(urlopen=urlopen, sleep=lambda _seconds: None)
    monkeypatch.setattr(credential, "_read_keychain", lambda: copy.deepcopy(payload))
    written: dict = {}
    monkeypatch.setattr(
        credential,
        "_write_keychain",
        lambda new_payload: written.setdefault("payload", copy.deepcopy(new_payload)),
    )
    return credential, written


def test_refresh_sends_claude_cli_user_agent_and_persists_the_rotation(monkeypatch):
    seen: list[dict] = []

    def urlopen(request, timeout=None):
        seen.append({key.lower(): value for key, value in request.headers.items()})
        return _FakeOAuthResponse(
            {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
        )

    credential, written = _credential(monkeypatch, _keychain_payload(expires_in_ms=-1000), urlopen)

    assert credential.token() == "new-access"
    # `claude-code/<version>` is answered with 429 and an unknown agent with
    # Cloudflare 403 Error 1010, so only the claude-cli form reaches the handler.
    assert seen[0]["user-agent"] == CLAUDE_CODE_USER_AGENT
    assert seen[0]["user-agent"].startswith("claude-cli/")
    assert "claude-code/" not in seen[0]["user-agent"]
    oauth = written["payload"]["claudeAiOauth"]
    assert oauth["accessToken"] == "new-access"
    assert oauth["refreshToken"] == "new-refresh"
    assert oauth["expiresAt"] > time.time() * 1000


def test_refresh_retries_a_rate_limited_attempt(monkeypatch):
    attempts: list[int] = []
    slept: list[float] = []

    def urlopen(request, timeout=None):
        attempts.append(1)
        if len(attempts) == 1:
            raise _http_error(429, b'{"error": {"type": "rate_limit_error"}}')
        return _FakeOAuthResponse({"access_token": "new-access", "expires_in": 3600})

    credential = ClaudeCodeCredential(urlopen=urlopen, sleep=slept.append)
    monkeypatch.setattr(
        credential, "_read_keychain", lambda: _keychain_payload(expires_in_ms=-1000)
    )
    monkeypatch.setattr(credential, "_write_keychain", lambda payload: None)

    assert credential.token() == "new-access"
    assert len(attempts) == 2
    assert slept == [2.0]


def test_refresh_reports_dead_and_blocked_credentials_clearly(monkeypatch):
    def dead(request, timeout=None):
        raise _http_error(400, b'{"error": "invalid_grant"}')

    credential, written = _credential(monkeypatch, _keychain_payload(expires_in_ms=-1000), dead)
    with pytest.raises(CredentialError, match="run `claude` and /login"):
        credential.token()
    assert written == {}

    def blocked(request, timeout=None):
        raise _http_error(403, b'{"title": "Error 1010: Access denied"}')

    credential, _ = _credential(monkeypatch, _keychain_payload(expires_in_ms=-1000), blocked)
    with pytest.raises(CredentialError, match="Cloudflare"):
        credential.token()


def test_failed_refresh_does_not_hammer_the_endpoint(monkeypatch):
    attempts: list[int] = []

    def urlopen(request, timeout=None):
        attempts.append(1)
        raise _http_error(429, b'{"error": {"type": "rate_limit_error"}}')

    credential = ClaudeCodeCredential(urlopen=urlopen, sleep=lambda _seconds: None)
    monkeypatch.setattr(
        credential, "_read_keychain", lambda: _keychain_payload(expires_in_ms=-1000)
    )
    monkeypatch.setattr(credential, "_write_keychain", lambda payload: None)

    with pytest.raises(CredentialError):
        credential.token()
    first_round = len(attempts)
    assert first_round == 3
    with pytest.raises(CredentialError, match="rate limiting"):
        credential.token()
    assert len(attempts) == first_round, "the cooldown should stop further attempts"
    assert REFRESH_FAILURE_COOLDOWN_SECONDS > 0


def test_still_valid_token_is_used_when_the_refresh_fails(monkeypatch):
    def urlopen(request, timeout=None):
        raise _http_error(429, b'{"error": {"type": "rate_limit_error"}}')

    payload = _keychain_payload(expires_in_ms=60_000, access="still-good")
    remaining = int(payload["claudeAiOauth"]["expiresAt"]) - time.time() * 1000
    assert remaining < CREDENTIAL_EXPIRY_SKEW_MS
    credential, _ = _credential(monkeypatch, payload, urlopen)

    assert credential.token() == "still-good"


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
    # Codex refuses view_image and strips attachments unless the descriptor
    # advertises image input.
    assert descriptor["input_modalities"] == ["text", "image"]
    assert descriptor["model_messages"]["instructions_template"] == ""


def test_bridge_removes_codex_identity_from_forwarded_instructions():
    guidance = (
        "You are Codex, an agent based on GPT-6. You and the user share one workspace.\n"
        "\n# Personality\nAs Codex, you are curious and careful.\n"
        "Use the `codex_apps` MCP when needed.\n"
    )
    neutral = neutralize_codex_identity(guidance)
    assert neutral == (
        "You and the user share one workspace.\n\n# Personality\n"
        "You are curious and careful.\nUse the `codex_apps` MCP when needed.\n"
    )
    assert neutralize_codex_identity("You are ChatGPT, a large language model. Be helpful.\n") == (
        "Be helpful.\n"
    )
    payload, _, _ = translate_request(
        {"model": DEFAULT_MODEL, "instructions": guidance, "input": []},
        mode="claude-code",
    )
    assert payload["system"][-1]["text"] == neutral
    assert payload["system"][1]["text"].startswith("You are Claude Code,")


def test_bridge_neutralizes_model_switch_guidance_without_rewriting_user_text():
    switch = (
        "The user was previously using a different model. Please continue the conversation "
        "according to the following instructions:\n\n"
        "You are Codex, an agent based on GPT-6. Follow the user's task."
    )
    payload, _, _ = translate_request(
        {
            "model": DEFAULT_MODEL,
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [
                        {"type": "input_text", "text": switch},
                    ],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "The file says You are Codex."},
                    ],
                },
            ],
        },
        mode="api-key",
    )
    assert "You are Codex" not in payload["messages"][0]["content"][0]["text"]
    assert "Follow the user's task." in payload["messages"][0]["content"][0]["text"]
    assert payload["messages"][1]["content"][0]["text"] == "The file says You are Codex."


def test_tool_result_keeps_an_image_returned_by_view_image():
    payload, _, _ = translate_request(
        {
            "model": DEFAULT_MODEL,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "look at this"}],
                },
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "toolu_1",
                    "name": "view_image",
                    "arguments": '{"path": "/tmp/shot.png"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "toolu_1",
                    "output": [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,QUJD",
                        }
                    ],
                },
            ],
        },
        mode="claude-code",
    )

    tool_result = payload["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["content"] == [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
        }
    ]


def _image_url(size, image_format="PNG", mode="RGB"):
    buffer = io.BytesIO()
    Image.new(mode, size).save(buffer, format=image_format)
    media = {"PNG": "png", "JPEG": "jpeg", "WEBP": "webp", "GIF": "gif"}[image_format]
    return f"data:image/{media};base64,{base64.b64encode(buffer.getvalue()).decode()}"


@pytest.mark.parametrize("count,expected", [(20, (2048, 1024)), (21, (2000, 1000))])
def test_many_image_limit_counts_history_and_nested_tool_results(count, expected):
    small = {"type": "input_image", "image_url": _image_url((8, 8))}
    large = {"type": "input_image", "image_url": _image_url((2048, 1024))}
    portrait = {"type": "input_image", "image_url": _image_url((1024, 2048))}
    body = {
        "input": [
            {"type": "message", "role": "user", "content": [large, *[small] * (count - 2)]},
            {"type": "function_call", "call_id": "shot", "name": "view_image", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "shot", "output": [portrait]},
        ],
    }
    original = copy.deepcopy(body)
    payload, _, _ = translate_request(body)
    prompt_source = payload["messages"][0]["content"][0]["source"]
    tool_source = payload["messages"][-1]["content"][0]["content"][0]["source"]
    with Image.open(io.BytesIO(base64.b64decode(prompt_source["data"]))) as img:
        assert img.size == expected
    with Image.open(io.BytesIO(base64.b64decode(tool_source["data"]))) as img:
        assert img.size == expected[::-1]
    assert len(payload["messages"][0]["content"]) + 1 == count
    assert (
        payload["messages"][0]["content"][1]["source"]["data"] == small["image_url"].split(",")[1]
    )
    assert body == original
    if count == 20:
        assert prompt_source["data"] == large["image_url"].split(",")[1]


@pytest.mark.parametrize(
    "image_format,mode", [("PNG", "RGBA"), ("JPEG", "RGB"), ("WEBP", "RGB"), ("GIF", "P")]
)
def test_many_image_resize_keeps_supported_formats(image_format, mode):
    url = _image_url((2001, 400), image_format, mode)
    payload, _, _ = translate_request(
        {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": url}] * 21,
                }
            ]
        }
    )
    source = payload["messages"][0]["content"][0]["source"]
    with Image.open(io.BytesIO(base64.b64decode(source["data"]))) as img:
        assert img.size == (2000, 400)
        assert source["media_type"] == Image.MIME[img.format]
        if mode == "RGBA":
            assert img.mode == "RGBA"


def test_single_image_is_clamped_to_anthropic_absolute_dimension_limit():
    payload, _, _ = translate_request(
        {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": _image_url((8001, 8))}],
                }
            ]
        }
    )
    source = payload["messages"][0]["content"][0]["source"]
    with Image.open(io.BytesIO(base64.b64decode(source["data"]))) as img:
        assert img.size == (8000, 8)


def test_tool_result_stays_text_when_no_image_is_present():
    payload, _, _ = translate_request(
        {
            "model": DEFAULT_MODEL,
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "toolu_1",
                    "output": [{"type": "input_text", "text": "hello"}],
                }
            ],
        },
        mode="claude-code",
    )

    assert payload["messages"][-1]["content"][0]["content"] == "hello"


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
        "id": f"msg_{stream.item_scope}_1",
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
        "id": f"fc_{stream.item_scope}_1",
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


def test_stream_completes_text_item_before_the_following_tool_call():
    """Codex records items in arrival order, so the preamble must close first."""
    stream = ResponsesStream("resp_order", "claude-sonnet-5")
    events = []
    events += stream.feed("message_start", {"message": {"usage": {"input_tokens": 1}}})
    events += stream.feed("content_block_start", {"index": 0, "content_block": {"type": "text"}})
    events += stream.feed(
        "content_block_delta",
        {"index": 0, "delta": {"type": "text_delta", "text": "Checking the repo."}},
    )
    events += stream.feed("content_block_stop", {"index": 0})
    events += stream.feed(
        "content_block_start",
        {"index": 1, "content_block": {"type": "tool_use", "id": "toolu_a", "name": "exec"}},
    )
    events += stream.feed(
        "content_block_delta",
        {"index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
    )
    events += stream.feed("content_block_stop", {"index": 1})
    events += stream.feed("message_stop", {})

    payloads = [json.loads(chunk.decode().split("data: ", 1)[1]) for chunk in events]
    kinds = [payload["type"] for payload in payloads]
    assert kinds == [
        "response.created",
        "response.output_item.added",
        "response.output_text.delta",
        "response.output_item.done",
        "response.output_item.added",
        "response.output_item.done",
        "response.completed",
    ]
    done = payloads[3]
    assert done["item"]["type"] == "message"
    assert done["item"]["content"] == [{"type": "output_text", "text": "Checking the repo."}]
    # The message item is emitted once, ahead of the tool call, with its own index.
    assert [payload["output_index"] for payload in payloads if "output_index" in payload] == [
        0,
        0,
        0,
        1,
        1,
    ]
    completed = payloads[-1]["response"]["output"]
    assert [item["type"] for item in completed] == ["message", "function_call"]


def test_stream_item_ids_are_unique_across_requests():
    first = ResponsesStream("resp_alpha", "claude-sonnet-5")
    second = ResponsesStream("resp_beta", "claude-sonnet-5")
    for stream in (first, second):
        stream.feed("content_block_start", {"index": 0, "content_block": {"type": "text"}})
        stream.feed(
            "content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "hi"}}
        )
        stream.feed("content_block_stop", {"index": 0})

    assert first.items[0]["id"] != second.items[0]["id"]


def test_trailing_assistant_turn_is_repaired_for_anthropic():
    payload, _, _ = translate_request(
        {
            "model": DEFAULT_MODEL,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "ship it"}],
                },
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "toolu_1",
                    "name": "exec_command",
                    "arguments": '{"cmd": "ls"}',
                },
            ],
        },
        mode="claude-code",
    )

    assert [message["role"] for message in payload["messages"]] == ["user", "assistant", "user"]
    recovered = payload["messages"][-1]["content"]
    assert recovered == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": "Tool output unavailable: the previous turn was interrupted.",
            "is_error": True,
        }
    ]


def test_trailing_assistant_text_gets_a_continuation_turn():
    payload, _, _ = translate_request(
        {
            "model": DEFAULT_MODEL,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "ship it"}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Reading the files next."}],
                },
            ],
        },
        mode="claude-code",
    )

    assert [message["role"] for message in payload["messages"]] == ["user", "assistant", "user"]
    assert payload["messages"][-1]["content"] == [{"type": "text", "text": "Continue."}]


_UNIFIED_HEADERS = {
    "anthropic-ratelimit-unified-status": "allowed",
    "anthropic-ratelimit-unified-representative-claim": "five_hour",
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-5h-utilization": "0.74",
    "anthropic-ratelimit-unified-5h-reset": "1790460000",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-7d-utilization": "0.08",
    "anthropic-ratelimit-unified-7d-reset": "1790942400",
}


def test_unified_rate_limits_become_codex_limit_headers():
    """Anthropic fractions must reach Codex as percentages and unix seconds."""
    snapshot = parse_unified_rate_limits(_UNIFIED_HEADERS)

    assert snapshot is not None
    assert snapshot["windows"]["five_hour"] == {
        "used_percent": 74.0,
        "window_minutes": 300,
        "resets_at": 1790460000,
        "status": "allowed",
    }
    assert snapshot["windows"]["seven_day"]["used_percent"] == 8.0
    assert snapshot["windows"]["seven_day"]["window_minutes"] == 10080

    assert codex_rate_limit_headers(snapshot) == {
        "x-claude-limit-name": "Claude",
        "x-claude-primary-used-percent": "74",
        "x-claude-primary-window-minutes": "300",
        "x-claude-primary-reset-at": "1790460000",
        "x-claude-secondary-used-percent": "8",
        "x-claude-secondary-window-minutes": "10080",
        "x-claude-secondary-reset-at": "1790942400",
    }
    assert describe_usage(snapshot) == "5h 74%; 7d 8%"


def test_unified_rate_limits_accept_percentages_and_partial_windows():
    """A value above 1 is already a percentage, and a missing window is omitted."""
    snapshot = parse_unified_rate_limits({"anthropic-ratelimit-unified-5h-utilization": "83.5"})

    assert snapshot is not None
    assert snapshot["windows"]["five_hour"]["used_percent"] == 83.5
    assert "seven_day" not in snapshot["windows"]
    headers = codex_rate_limit_headers(snapshot)
    assert headers["x-claude-primary-used-percent"] == "83.5"
    # A single window must not claim an empty secondary slot.
    assert "x-claude-secondary-used-percent" not in headers
    assert parse_unified_rate_limits({}) is None
    assert codex_rate_limit_headers(None) == {}


def test_usage_snapshots_are_recorded_and_read_back(tmp_path):
    path = tmp_path / "claude-usage.json"
    snapshot = parse_unified_rate_limits(_UNIFIED_HEADERS)
    assert snapshot is not None

    assert record_usage_snapshot(snapshot, path) == path
    state = read_usage_state(path)
    assert state["snapshot"] == snapshot
    assert state["history"][-1]["five_hour"] == 74.0
    assert state["history"][-1]["seven_day"] == 8.0

    record_usage_snapshot({"windows": {"five_hour": {"used_percent": 79.0}}}, path)
    state = read_usage_state(path)
    assert [sample["five_hour"] for sample in state["history"]] == [74.0, 79.0]
    # Tracking must never raise into a turn, even when the parent directory is new.
    assert record_usage_snapshot({"windows": {}}, tmp_path / "nested" / "state.json") is not None
    assert read_usage_state(tmp_path / "absent.json") == {}


def test_subscription_usage_rows_cover_weekly_and_scoped_limits():
    payload = {
        "limits": [
            {
                "kind": "session",
                "group": "session",
                "percent": 79,
                "severity": "warning",
                "resets_at": "2026-09-26T22:00:00Z",
                "is_active": True,
            },
            {
                "kind": "weekly_all",
                "group": "weekly",
                "percent": 9,
                "severity": "normal",
                "resets_at": "2026-10-02T12:00:00Z",
                "is_active": False,
            },
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 0,
                "severity": "normal",
                "resets_at": "2026-10-02T12:00:00Z",
                "is_active": False,
                "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
            },
        ]
    }

    rows = summarize_subscription_usage(payload)

    assert [row["label"] for row in rows] == [
        "5h session",
        "weekly (all models)",
        "weekly (Fable)",
    ]
    assert rows[0]["percent"] == 79.0
    assert rows[0]["is_active"] is True
    assert summarize_subscription_usage({"five_hour": {"utilization": 12}})[0]["percent"] == 12.0


def test_subscription_usage_needs_the_subscription_credential():
    class _KeyCredential:
        mode = "api-key"

        def token(self):
            return "fake-key"

        def invalidate(self):
            return

    with pytest.raises(CredentialError):
        fetch_subscription_usage(_KeyCredential())


def test_subscription_usage_can_use_the_current_token_without_refresh(monkeypatch):
    credential = ClaudeCodeCredential()
    monkeypatch.setattr(credential, "current_token", lambda: "current-token")
    monkeypatch.setattr(credential, "token", lambda: pytest.fail("unexpected token refresh"))

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    requests = []

    def urlopen(request, timeout):
        requests.append((request, timeout))
        return _Response(b'{"limits": []}')

    payload = fetch_subscription_usage(credential, refresh=False, urlopen=urlopen)

    assert payload == {"limits": []}
    request, timeout = requests[0]
    assert request.get_header("Authorization") == "Bearer current-token"
    assert timeout == 30


class _FakeCredential:
    mode = "api-key"

    def token(self):
        return "fake-key"

    def invalidate(self):
        return


@pytest.fixture
def bridge_server(tmp_path, monkeypatch):
    """A live bridge with a stubbed stream and isolated capture/account state."""
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path / "agentroute"))
    monkeypatch.setenv("AGENTROUTE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AGENTROUTE_PROFILE_CAPTURE", raising=False)
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
        if captured.get("rate_limits") is not None:
            yield RATE_LIMIT_EVENT, captured["rate_limits"]
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


def test_http_bridge_selects_a_profile_and_records_its_usage(bridge_server, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path))
    server, captured = bridge_server
    config = default_config()
    config.claude_subscriptions.profiles["second"] = ClaudeSubscriptionProfile(
        config_dir=str(tmp_path / "claude-accounts" / "second"),
        auth_generation="login-1",
        priority=1,
    )
    config.claude_subscriptions.active_profile = "second"
    monkeypatch.setattr("agentroute.config.load_config", lambda: config)
    monkeypatch.setattr("agentroute.claude_profile_pool.load_config", lambda: config)

    def account_identity(profile):
        return f"account-{Path(profile.config_dir).name if profile.config_dir else 'default'}"

    monkeypatch.setattr(
        "agentroute.claude_profiles.profile_account_identity",
        account_identity,
    )
    monkeypatch.setattr("agentroute.claude_profile_pool.profile_account_identity", account_identity)

    def saved_credential(name, profile):
        identity = account_identity(profile)
        return SimpleNamespace(
            mode="claude-code",
            profile_name=name,
            profile_scope=profile_scope(name, profile),
            account_identity=identity,
            legacy_profile_digests=(hashlib.sha256(name.encode()).hexdigest()[:16],),
            usage_path=profile_usage_path(name, identity),
            _read_keychain=lambda: {"claudeAiOauth": {"refreshToken": "available"}},
        )

    monkeypatch.setattr("agentroute.claude_profiles.ProfileCredential", saved_credential)
    monkeypatch.setattr("agentroute.claude_profile_pool.ProfileCredential", saved_credential)

    BridgeHandler.credentials = ClaudeProfilePool()
    captured["rate_limits"] = parse_unified_rate_limits(_UNIFIED_HEADERS)
    streams = []

    def fake_stream(credentials, payload):
        streams.append((credentials, payload))
        if captured.get("rate_limits") is not None:
            yield RATE_LIMIT_EVENT, captured["rate_limits"]
        yield "message_start", {"message": {"usage": {"input_tokens": 5}}}
        if len(streams) == 1:
            yield (
                "content_block_start",
                {
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                },
            )
            yield (
                "content_block_delta",
                {
                    "index": 0,
                    "delta": {"type": "signature_delta", "signature": "sig-second"},
                },
            )
            yield "content_block_stop", {"index": 0}
        yield "content_block_start", {"index": 1, "content_block": {"type": "text"}}
        yield (
            "content_block_delta",
            {
                "index": 1,
                "delta": {"type": "text_delta", "text": "ok"},
            },
        )
        yield "content_block_stop", {"index": 1}
        yield "message_delta", {"usage": {"output_tokens": 1}}
        yield "message_stop", {}

    BridgeHandler.stream_factory = staticmethod(fake_stream)
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/v1/responses"

    def post(body):
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json", "thread-id": "thread-2"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode()

    first_body = {
        "model": "claude-haiku-5-5",
        "instructions": "Be concise.",
        "tools": [],
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "first"}],
            }
        ],
    }
    first_payload = post(first_body)
    affinity = json.loads((tmp_path / "state" / "claude-profile-affinity.json").read_text())
    assert affinity["version"] == 2
    assert affinity["threads"][0][3] is True
    first_events = [
        json.loads(line[6:]) for line in first_payload.splitlines() if line.startswith("data: ")
    ]
    reasoning = next(
        event["item"]
        for event in first_events
        if event.get("type") == "response.output_item.done"
        and event["item"].get("type") == "reasoning"
    )
    second_body = {
        **first_body,
        "input": [
            *first_body["input"],
            reasoning,
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "continue"}],
            },
        ],
    }
    second_payload = post(second_body)
    usage_request = urllib.request.Request(url.rsplit("/", 1)[0] + "/usage?profile=second")
    with urllib.request.urlopen(usage_request, timeout=5) as response:
        usage_payload = json.loads(response.read())

    assert "event: response.completed" in first_payload
    assert "event: response.completed" in second_payload
    assert [credential.profile_name for credential, _payload in streams] == ["second", "second"]
    replayed_content = [
        block for message in streams[1][1]["messages"] for block in message["content"]
    ]
    assert any(block["type"] == "thinking" for block in replayed_content)
    assert (
        next(block for block in replayed_content if block["type"] == "thinking")["signature"]
        == "sig-second"
    )
    second_config_dir = config.claude_subscriptions.profiles["second"].config_dir
    state_path = profile_usage_path("second", f"account-{Path(second_config_dir).name}")
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert state["snapshot"]["windows"]["five_hour"]["used_percent"] == 74.0
    assert usage_payload["profile"] == "second"
    assert usage_payload["snapshot"]["windows"]["five_hour"]["used_percent"] == 74.0


def test_http_bridge_serves_models_and_health(bridge_server):
    server, _ = bridge_server
    host, port = server.server_address[:2]

    with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=30) as response:
        assert json.loads(response.read()) == {"status": "ok"}

    with urllib.request.urlopen(f"http://{host}:{port}/v1/models", timeout=30) as response:
        catalog = json.loads(response.read())
    assert catalog["data"] == [{"id": "claude-sonnet-5"}]
    assert catalog["models"][0]["slug"] == "claude-sonnet-5"


def test_http_bridge_emits_and_records_claude_limits(bridge_server, tmp_path, monkeypatch):
    """Codex reads the limit family off the stream, so the bridge must send it there."""
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path))
    server, captured = bridge_server
    host, port = server.server_address[:2]
    captured["rate_limits"] = parse_unified_rate_limits(_UNIFIED_HEADERS)
    request = urllib.request.Request(
        f"http://{host}:{port}/v1/responses",
        data=json.dumps({"model": "claude-sonnet-5", "input": []}).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read().decode()
        headers = response.headers

    assert response.status == 200
    assert "event: response.completed" in payload
    assert headers["x-claude-limit-name"] == "Claude"
    assert headers["x-claude-primary-used-percent"] == "74"
    assert headers["x-claude-primary-window-minutes"] == "300"
    assert headers["x-claude-primary-reset-at"] == "1790460000"
    assert headers["x-claude-secondary-used-percent"] == "8"
    assert headers["x-claude-secondary-window-minutes"] == "10080"

    state = json.loads((tmp_path / "state" / "claude-usage.json").read_text())
    assert state["snapshot"]["windows"]["five_hour"]["used_percent"] == 74.0

    with urllib.request.urlopen(f"http://{host}:{port}/v1/usage", timeout=30) as response:
        reported = json.loads(response.read())
    assert reported["snapshot"]["windows"]["seven_day"]["used_percent"] == 8.0
    assert reported["history"][-1]["five_hour"] == 74.0


def test_http_bridge_omits_limit_headers_without_usage(bridge_server, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path))
    server, _ = bridge_server
    host, port = server.server_address[:2]
    request = urllib.request.Request(
        f"http://{host}:{port}/v1/responses",
        data=json.dumps({"model": "claude-sonnet-5", "input": []}).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()
        headers = response.headers

    assert "x-claude-primary-used-percent" not in headers
    assert not (tmp_path / "state" / "claude-usage.json").exists()


def test_anthropic_429_keeps_upstream_limit_headers():
    error = urllib.error.HTTPError(
        "https://api.anthropic.com/v1/messages",
        429,
        "rate limited",
        {
            "anthropic-ratelimit-unified-5h-utilization": "1",
            "anthropic-ratelimit-unified-5h-reset": "1790501400",
        },
        io.BytesIO(b'{"type":"error","error":{"type":"rate_limit_error"}}'),
    )

    def rejected(_request, timeout):
        assert timeout == 900
        raise error

    with pytest.raises(UpstreamRateLimitError) as raised:
        next(anthropic_stream(_FakeCredential(), {"model": "claude-sonnet-5"}, urlopen=rejected))
    assert raised.value.snapshot["windows"]["five_hour"]["used_percent"] == 100


def test_http_bridge_subscription_limit_is_terminal(bridge_server, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path))
    server, _ = bridge_server
    host, port = server.server_address[:2]
    monkeypatch.setattr(_FakeCredential, "mode", "claude-code")
    snapshot = {
        "windows": {
            "five_hour": {"used_percent": 100, "window_minutes": 300, "resets_at": 1790501400},
            "seven_day": {"used_percent": 19, "window_minutes": 10080, "resets_at": 1790942400},
        }
    }

    def rejected(_credentials, _payload):
        raise UpstreamRateLimitError("anthropic 429", snapshot)
        yield  # pragma: no cover - keep this a generator like the real stream

    BridgeHandler.stream_factory = staticmethod(rejected)
    request = urllib.request.Request(
        f"http://{host}:{port}/v1/responses",
        data=json.dumps({"model": "claude-sonnet-5", "input": []}).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(request, timeout=30)

    assert raised.value.code == 429
    assert raised.value.headers["x-codex-active-limit"] == "claude"
    assert raised.value.headers["x-claude-primary-used-percent"] == "100"
    assert json.load(raised.value)["error"]["type"] == "usage_limit_reached"
    assert read_usage_state()["snapshot"] == snapshot


def test_http_bridge_subscription_429_without_windows_still_stops(bridge_server, monkeypatch):
    monkeypatch.setattr(_FakeCredential, "mode", "claude-code")
    server, _ = bridge_server
    host, port = server.server_address[:2]

    def rejected(_credentials, _payload):
        raise UpstreamRateLimitError("anthropic 429", None)
        yield  # pragma: no cover

    BridgeHandler.stream_factory = staticmethod(rejected)
    request = urllib.request.Request(
        f"http://{host}:{port}/v1/responses",
        data=b'{"model":"claude-sonnet-5","input":[]}',
        headers={"content-type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(request, timeout=30)
    assert raised.value.code == 429
    assert raised.value.headers["x-codex-active-limit"] == "claude"
    assert raised.value.headers["x-claude-limit-name"] == "Claude"
    assert json.load(raised.value)["error"] == {"type": "usage_limit_reached"}


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


def test_stream_preserves_cache_receipts_and_cumulative_usage():
    stream = ResponsesStream("resp_cache", "claude-opus-5-5")
    stream.feed(
        "message_start",
        {
            "message": {
                "model": "claude-opus-5-5",
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 20,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 15,
                        "ephemeral_1h_input_tokens": 5,
                    },
                },
            }
        },
    )
    stream.feed("message_delta", {"usage": {"output_tokens": 3}})
    stream.feed("message_delta", {"usage": {"output_tokens": 8}})
    events = stream.feed("message_stop", {})
    usage = json.loads(events[-1].decode().split("data: ", 1)[1])["response"]["usage"]
    assert usage == {
        "input_tokens": 130,
        "input_tokens_details": {"cached_tokens": 100, "cache_write_tokens": 20},
        "output_tokens": 8,
        "output_tokens_details": None,
        "total_tokens": 138,
    }
    assert stream.usage["cache_write_1h_input_tokens"] == 5
    assert stream.actual_model == "claude-opus-5-5"


def test_http_bridge_profiles_translated_request_without_prompt_text(
    bridge_server, tmp_path, monkeypatch
):
    from agentroute.profiling import read_profiles, set_capture

    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTROUTE_STATE_DIR", str(tmp_path / "state"))
    set_capture(True)
    server, captured = bridge_server
    host, port = server.server_address[:2]
    body = {
        "model": "claude-sonnet-5",
        "instructions": "private instruction",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "private user message"}],
            }
        ],
        "tools": [],
    }
    request = urllib.request.Request(
        f"http://{host}:{port}/v1/responses",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "session-id": "s",
            "x-codex-inference-call-id": "i1",
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()
    # The handler records before closing the response's chunked stream.
    rows, errors = read_profiles(tmp_path / "profiling")
    assert errors == 0 and len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "completed"
    assert row["usage"]["input_tokens"] == 5
    assert row["usage"]["output_tokens"] == 1
    assert row["inference_call_id"] == "i1"
    assert row["context"]["components"]["user_messages"]["bytes"] > 0
    assert row["harness_context"]["components"]["base_instructions"]["bytes"] > 0
    assert "private instruction" not in json.dumps(rows)
    assert "private user message" not in json.dumps(rows)


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"])
@pytest.mark.parametrize(
    "effort,expected",
    [
        ("medium", "medium"),
        ("high", "high"),
        ("xhigh", "xhigh"),
        ("ultra", "max"),
        ("persistent", "max"),
        ("none", "low"),
    ],
)
def test_modern_claude_effort_and_adaptive_thinking(model, effort, expected):
    payload, _, _ = translate_request({"model": model, "reasoning": {"effort": effort}})
    assert payload["output_config"] == {"effort": expected}
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["max_tokens"] == 128000


def test_explicit_output_cap_and_haiku_effort():
    payload, _, _ = translate_request({"model": "claude-sonnet-5-5", "max_output_tokens": 4096})
    assert payload["max_tokens"] == 4096
    haiku, _, _ = translate_request(
        {"model": "claude-haiku-4-5-20251001", "reasoning": {"effort": "low"}}
    )
    assert "output_config" not in haiku
    assert "thinking" not in haiku


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"])
@pytest.mark.parametrize("choice", ["required", {"type": "function", "name": "read.file"}])
def test_modern_claude_uses_auto_for_required_tools(model, choice):
    body = {
        "model": model,
        "tool_choice": choice,
        "tools": [
            {"type": "function", "name": "read.file", "parameters": {"type": "object"}},
            {"type": "function", "name": "other", "parameters": {"type": "object"}},
        ],
    }
    before = copy.deepcopy(body)
    payload, _, _ = translate_request(body)
    assert payload["tool_choice"] == {"type": "auto"}
    assert "must call" in payload["system"][-1]["text"]
    if isinstance(choice, dict):
        assert [tool["name"] for tool in payload["tools"]] == ["read_file"]
    assert body == before


def test_effort_catalog_and_opt_in_fable():
    from agentroute.providers import ensure_claude_bridge_backend

    config = default_config()
    ensure_claude_bridge_backend(config, 8090)
    models = catalog_from_config(config)
    assert ("claude-fable-5-1", 200000) in models
    descriptors = {entry["slug"]: entry for entry in model_catalog(models)["models"]}
    for name in ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"]:
        assert descriptors[name]["supports_reasoning_effort_updates"] is True
        assert [level["effort"] for level in descriptors[name]["supported_reasoning_levels"]] == [
            "low",
            "medium",
            "high",
            "xhigh",
            "ultra",
        ]
    assert descriptors["claude-haiku-5-5"]["context_window"] == 1_000_000
