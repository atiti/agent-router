import copy
import json
import urllib.request
from pathlib import Path

import pytest
import tomllib
from test_claude_bridge import bridge_server as native_bridge_server  # noqa: F401

from agentroute.claude_bridge import BridgeHandler, translate_request
from agentroute.claude_thinking_store import ThinkingReplayError
from agentroute.claude_tool_search import validate_search_block
from agentroute.config import ClaudeCacheConfig, default_config
from agentroute.providers import ensure_claude_bridge_backend, sync_codex_providers


def large_request():
    return {
        "model": "claude-sonnet-5-5",
        "input": [{"type": "message", "role": "user", "content": "Find the invoice."}],
        "tools": [
            {
                "type": "function",
                "name": f"mcp_invoice_{i}",
                "description": "Find invoices. " * 100,
                "parameters": {"type": "object", "properties": {}},
            }
            for i in range(449)
        ]
        + [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}],
    }


def test_large_catalog_keeps_all_schemas_but_loads_only_core_tools():
    body = large_request()
    original = copy.deepcopy(body)
    payload, _, _ = translate_request(body, cache_config=ClaudeCacheConfig(ttl="1h"))
    deferred = [tool for tool in payload["tools"] if tool.get("defer_loading")]
    assert [tool["name"] for tool in deferred] == [f"mcp_invoice_{i}" for i in range(449)]
    assert all("cache_control" not in tool for tool in deferred)
    assert [tool["name"] for tool in payload["tools"] if not tool.get("defer_loading")] == [
        "exec_command",
        "tool_search_tool_regex",
    ]
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert body == original


@pytest.mark.parametrize(
    "change",
    [
        {"tool_choice": "required", "model": "claude-haiku-5-5"},
        {"model": "claude-sonnet-4-5"},
        {"tool_choice": {"type": "function", "name": "exec_command"}},
    ],
)
def test_forced_or_unsupported_catalog_retains_existing_behavior(change):
    payload, _, _ = translate_request({**large_request(), **change})
    assert all("defer_loading" not in tool for tool in payload["tools"])
    assert all(tool["name"] != "tool_search_tool_regex" for tool in payload["tools"])


def test_codex_catalog_contains_claude_limits_without_pricing_aliases(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path))
    monkeypatch.setattr(
        "agentroute.providers._installed_codex_catalog",
        lambda source: {
            "models": [
                {
                    "slug": "gpt-baseline",
                    "context_window": 272_000,
                    "auto_compact_token_limit": 100_000,
                }
            ]
        },
    )
    config = default_config()
    ensure_claude_bridge_backend(config, 8090)
    config_path = tmp_path / "config.toml"
    sync_codex_providers(config, config_path, backup=False)
    catalog_path = Path(tomllib.loads(config_path.read_text())["model_catalog_json"])
    catalog = json.loads(catalog_path.read_text())
    selected = next(model for model in catalog["models"] if model["slug"] == "claude-sonnet-5-5")
    assert {
        key: selected[key]
        for key in (
            "context_window",
            "max_context_window",
            "auto_compact_token_limit",
            "effective_context_window_percent",
        )
    } == {
        "context_window": 1_000_000,
        "max_context_window": 1_000_000,
        "auto_compact_token_limit": None,
        "effective_context_window_percent": 95,
    }


@pytest.mark.parametrize("reference_count", [1, 50])
def test_native_search_replays_through_http_without_local_execution(
    request,
    monkeypatch,
    reference_count,
):
    server, _ = request.getfixturevalue("native_bridge_server")
    host, port = server.server_address[:2]
    forwarded = []
    search = {
        "type": "server_tool_use",
        "id": "srvtoolu_search",
        "name": "tool_search_tool_regex",
        "input": {"pattern": "invoice_42"},
    }
    result = {
        "type": "tool_search_tool_result",
        "tool_use_id": "srvtoolu_search",
        "content": {
            "type": "tool_search_tool_search_result",
            "tool_references": [
                {"type": "tool_reference", "tool_name": f"mcp_invoice_{i}"}
                for i in ([42] if reference_count == 1 else range(reference_count))
            ],
        },
    }

    def upstream(credentials, payload):
        forwarded.append(payload)
        yield "message_start", {"message": {"usage": {"input_tokens": 30}}}
        if len(forwarded) == 1:
            for index, block in enumerate(
                (
                    search,
                    result,
                    {
                        "type": "tool_use",
                        "id": "call_invoice",
                        "name": "mcp_invoice_42",
                        "input": {},
                    },
                )
            ):
                yield "content_block_start", {"index": index, "content_block": block}
                if index == 0:
                    yield (
                        "content_block_delta",
                        {
                            "index": index,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": '{"pattern":"invoice_42"}',
                            },
                        },
                    )
                yield "content_block_stop", {"index": index}
        else:
            yield "content_block_start", {"index": 0, "content_block": {"type": "text"}}
            yield (
                "content_block_delta",
                {"index": 0, "delta": {"type": "text_delta", "text": "Invoice found."}},
            )
            yield "content_block_stop", {"index": 0}
        yield "message_stop", {}

    monkeypatch.setattr(BridgeHandler, "stream_factory", staticmethod(upstream))

    def post(body):
        request = urllib.request.Request(
            f"http://{host}:{port}/v1/responses",
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            events = [
                json.loads(line[6:])
                for line in response.read().decode().splitlines()
                if line.startswith("data: ")
            ]
        return next(e["response"] for e in events if e["type"] == "response.completed")

    body = large_request()
    first = post(body)
    assert [item["type"] for item in first["output"]] == ["reasoning", "reasoning", "function_call"]
    assert all(len(json.dumps(item)) < 1_000 for item in first["output"][:2])
    second = post(
        {
            **body,
            "input": [
                *body["input"],
                *first["output"],
                {
                    "type": "function_call_output",
                    "call_id": "call_invoice",
                    "output": "invoice 42",
                },
            ],
        }
    )
    assert forwarded[1]["messages"][1]["content"][:2] == [search, result]
    assert second["output"][0]["content"][0]["text"] == "Invoice found."


def test_search_replay_refuses_oversized_provider_context():
    with pytest.raises(ThinkingReplayError, match="narrower search"):
        validate_search_block(
            {
                "type": "tool_search_tool_result",
                "content": {
                    "type": "tool_search_tool_search_result",
                    "tool_references": [
                        {"type": "tool_reference", "tool_name": f"mcp_invoice_{i}"}
                        for i in range(449)
                    ],
                },
            }
        )
