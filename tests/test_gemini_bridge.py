"""Tests for the direct Gemini API bridge."""

from __future__ import annotations

import json

import pytest

from agentroute import gemini_bridge as bridge
from agentroute.config import ModelCapabilities, default_config
from agentroute.gemini_cli import ensure_backend

MODEL = "gemini-2.5-flash"


def sse_chunk(_state: dict, **payload) -> bytes:
    return ("data: " + json.dumps(payload) + "\n\n").encode()


def collect(frames, tools=None):
    stream = bridge.ResponsesStream(MODEL, tools or {"shell": "function"})
    events = []
    for chunk in bridge.iter_sse(frames):
        for event in stream.feed(chunk):
            events.append(event.decode())
    for event in stream.complete():
        events.append(event.decode())
    return events, stream


def names(events):
    return [event.split("\n", 1)[0].removeprefix("event: ") for event in events]


def completed_item(events):
    block = [event for event in events if event.startswith("event: response.completed")][-1]
    return json.loads(block.split("data: ", 1)[1])["response"]


FUNCTION_TOOL = {
    "type": "function",
    "name": "shell",
    "description": "Run a shell command.",
    "parameters": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}

CUSTOM_TOOL = {
    "type": "custom",
    "name": "apply_patch",
    "description": "Apply a patch.",
    "format": {"type": "grammar", "syntax": "lark", "definition": "start: /.+/"},
}


def test_translate_request_maps_instructions_and_turns():
    payload = bridge.translate_request(
        {
            "instructions": "You review code.",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "Check this."}]},
            ],
            "max_output_tokens": 256,
        },
        MODEL,
    )
    assert payload["systemInstruction"]["parts"][0]["text"] == "You review code."
    assert payload["contents"] == [{"role": "user", "parts": [{"text": "Check this."}]}]
    assert payload["generationConfig"]["maxOutputTokens"] == 256


def test_translate_request_keeps_assistant_role_as_model():
    payload = bridge.translate_request(
        {
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "Hi"}]},
                {"role": "assistant", "content": [{"type": "output_text", "text": "Hello"}]},
            ]
        },
        MODEL,
    )
    assert [turn["role"] for turn in payload["contents"]] == ["user", "model"]


def test_translate_request_replays_tool_calls_with_matching_names():
    payload = bridge.translate_request(
        {
            "input": [
                {
                    "type": "function_call",
                    "name": "shell",
                    "arguments": '{"command":"ls"}',
                    "call_id": "c1",
                },
                {"type": "function_call_output", "call_id": "c1", "output": "a.txt"},
            ]
        },
        MODEL,
    )
    call = payload["contents"][0]["parts"][0]["functionCall"]
    assert call == {"name": "shell", "args": {"command": "ls"}}
    response = payload["contents"][1]["parts"][0]["functionResponse"]
    assert response["name"] == "shell"
    assert response["response"]["output"] == "a.txt"


def test_custom_tool_output_replays_as_freeform_input():
    payload = bridge.translate_request(
        {
            "input": [
                {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "input": "*** Begin Patch",
                    "call_id": "c2",
                },
                {"type": "custom_tool_call_output", "call_id": "c2", "output": "Done"},
            ]
        },
        MODEL,
    )
    call = payload["contents"][0]["parts"][0]["functionCall"]
    assert call == {"name": "apply_patch", "args": {"input": "*** Begin Patch"}}
    assert payload["contents"][1]["parts"][0]["functionResponse"]["name"] == "apply_patch"


def test_tool_declarations_uppercase_types_and_expand_custom_tools():
    declarations = bridge.tool_declarations({"tools": [FUNCTION_TOOL, CUSTOM_TOOL]})
    shell = declarations[0]
    assert shell["parameters"]["type"] == "OBJECT"
    assert shell["parameters"]["properties"]["command"]["type"] == "STRING"
    patch = declarations[1]
    assert patch["name"] == "apply_patch"
    assert patch["parameters"]["required"] == ["input"]


def test_translate_request_declares_tools_and_required_choice():
    payload = bridge.translate_request(
        {"input": "go", "tools": [FUNCTION_TOOL], "tool_choice": "required"},
        MODEL,
    )
    assert payload["tools"][0]["functionDeclarations"][0]["name"] == "shell"
    assert payload["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"


def test_translate_request_honours_a_named_tool_choice():
    payload = bridge.translate_request(
        {
            "input": "go",
            "tools": [FUNCTION_TOOL],
            "tool_choice": {"type": "function", "name": "shell"},
        },
        MODEL,
    )
    config = payload["toolConfig"]["functionCallingConfig"]
    assert config["mode"] == "ANY"
    assert config["allowedFunctionNames"] == ["shell"]


def test_json_schema_output_requests_structured_content():
    payload = bridge.translate_request(
        {
            "input": "go",
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "answer",
                    "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                }
            },
        },
        MODEL,
    )
    generation = payload["generationConfig"]
    assert generation["responseMimeType"] == "application/json"
    assert generation["responseSchema"]["properties"]["ok"]["type"] == "BOOLEAN"


def test_thinking_config_matches_the_model_family():
    assert bridge.thinking_config("gemini-3-pro", "high") == {"thinkingLevel": "high"}
    assert bridge.thinking_config("gemini-3-flash", "low") == {"thinkingLevel": "low"}
    assert bridge.thinking_config("gemini-2.5-flash", "medium") == {"thinkingBudget": 4096}
    assert bridge.thinking_config("gemini-2.0-flash", "high") is None


def test_text_stream_becomes_output_text_events():
    events, _ = collect(
        [
            sse_chunk({}, candidates=[{"content": {"parts": [{"text": "Hel"}]}}]),
            sse_chunk(
                {}, candidates=[{"content": {"parts": [{"text": "lo"}]}, "finishReason": "STOP"}]
            ),
        ]
    )
    assert "response.output_text.delta" in names(events)
    assert completed_item(events)["output"][0]["content"][0]["text"] == "Hello"


def test_thought_parts_never_become_assistant_output():
    events, _ = collect(
        [
            sse_chunk(
                {},
                candidates=[
                    {
                        "content": {
                            "parts": [
                                {"text": "secret reasoning", "thought": True},
                                {"text": "Answer"},
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
            )
        ]
    )
    response = completed_item(events)
    assert [item["type"] for item in response["output"]] == ["message"]
    assert response["output"][0]["content"][0]["text"] == "Answer"


def test_native_function_call_becomes_a_function_call_item():
    events, _ = collect(
        [
            sse_chunk(
                {},
                candidates=[
                    {
                        "content": {
                            "parts": [
                                {"functionCall": {"name": "shell", "args": {"command": "ls"}}}
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
            )
        ]
    )
    item = completed_item(events)["output"][0]
    assert item["type"] == "function_call"
    assert item["name"] == "shell"
    assert json.loads(item["arguments"]) == {"command": "ls"}
    assert item["call_id"].startswith("call_")
    assert "response.function_call_arguments.done" in names(events)


def test_custom_tool_call_keeps_freeform_input():
    stream = bridge.ResponsesStream(MODEL, {"apply_patch": "custom"})
    chunk = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"functionCall": {"name": "apply_patch", "args": {"input": "*** Begin"}}}
                    ]
                }
            }
        ]
    }
    events = [event.decode() for event in stream.feed(chunk)]
    events += [event.decode() for event in stream.complete()]
    item = stream.result()["output"][0]
    assert item["type"] == "custom_tool_call"
    assert item["input"] == "*** Begin"
    assert "response.custom_tool_call_input.delta" in names(events)


def test_parallel_function_calls_keep_their_order():
    events, _ = collect(
        [
            sse_chunk(
                {},
                candidates=[
                    {
                        "content": {
                            "parts": [
                                {"functionCall": {"name": "shell", "args": {"command": "ls"}}},
                                {"functionCall": {"name": "shell", "args": {"command": "pwd"}}},
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
            )
        ]
    )
    calls = [item for item in completed_item(events)["output"] if item["type"] == "function_call"]
    assert [json.loads(call["arguments"])["command"] for call in calls] == ["ls", "pwd"]


def test_usage_metadata_is_mapped():
    stream = bridge.ResponsesStream(MODEL, {})
    chunk = {
        "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 3, "totalTokenCount": 14},
    }
    list(stream.feed(chunk))
    list(stream.complete())
    assert stream.result()["usage"] == {
        "input_tokens": 11,
        "output_tokens": 3,
        "total_tokens": 14,
    }


def test_usage_defaults_to_zero_without_metadata():
    assert bridge.usage_from(None) == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def test_iter_sse_skips_non_data_and_bad_frames():
    frames = [b'data: {"a":1}\n', b"\n", b"event: x\n", b"data: [DONE]\n", b"data: not-json\n"]
    assert list(bridge.iter_sse(frames)) == [{"a": 1}]


def test_stream_url_uses_the_gemini_api(monkeypatch):
    monkeypatch.setenv("AGENTROUTE_GEMINI_API_BASE", "https://example.test/v1beta")
    monkeypatch.delenv("AGENTROUTE_GEMINI_TRANSPORT", raising=False)
    assert bridge.stream_url("gemini-2.5-pro") == (
        "https://example.test/v1beta/models/gemini-2.5-pro:streamGenerateContent?alt=sse"
    )


def test_stream_url_escapes_vertex_identifiers(monkeypatch):
    monkeypatch.setenv("AGENTROUTE_GEMINI_TRANSPORT", "vertex")
    monkeypatch.setenv("AGENTROUTE_GEMINI_VERTEX_PROJECT", "my project")
    monkeypatch.setenv("AGENTROUTE_GEMINI_VERTEX_LOCATION", "europe-west4")
    assert bridge.stream_url("gemini-2.5-pro") == (
        "https://aiplatform.googleapis.com/v1/projects/my%20project"
        "/locations/europe-west4/publishers/google/models/gemini-2.5-pro"
        ":streamGenerateContent?alt=sse"
    )


def test_vertex_without_a_project_fails_closed(monkeypatch):
    monkeypatch.setenv("AGENTROUTE_GEMINI_TRANSPORT", "vertex")
    monkeypatch.delenv("AGENTROUTE_GEMINI_VERTEX_PROJECT", raising=False)
    with pytest.raises(bridge.CredentialError, match="VERTEX_PROJECT"):
        bridge.stream_url("gemini-2.5-pro")


def test_credential_selection_follows_the_transport(monkeypatch):
    monkeypatch.delenv("AGENTROUTE_GEMINI_TRANSPORT", raising=False)
    assert isinstance(bridge.credential_for_transport(), bridge.GeminiCredential)
    monkeypatch.setenv("AGENTROUTE_GEMINI_TRANSPORT", "vertex")
    assert isinstance(bridge.credential_for_transport(), bridge.GoogleTokenCredential)


def test_choose_models_prefers_stable_newest_chat_models():
    models = bridge.choose_models(
        [
            "models/gemini-2.5-flash",
            "models/gemini-2.5-pro",
            "models/gemini-3-pro-preview",
            "models/gemini-2.5-flash-lite",
            "models/gemini-embedding-001",
            "models/imagen-4.0-generate-001",
        ]
    )
    assert models["fast"] == "gemini-2.5-flash-lite"
    assert models["normal"] == "gemini-2.5-flash"
    # A newer preview outranks an older stable release for the reasoning tiers.
    assert models["smart"] == "gemini-3-pro-preview"
    assert models["max"] == "gemini-3-pro-preview"


def test_fallback_models_are_used_until_discovery(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTROUTE_GEMINI_MODELS", raising=False)
    monkeypatch.setenv("AGENTROUTE_GEMINI_MODELS_CACHE", str(tmp_path / "none.json"))
    assert bridge.model_map() == bridge.FALLBACK_MODELS


def test_discovered_models_are_cached(monkeypatch, tmp_path):
    cache = tmp_path / "models.json"
    monkeypatch.delenv("AGENTROUTE_GEMINI_MODELS", raising=False)
    monkeypatch.setenv("AGENTROUTE_GEMINI_MODELS_CACHE", str(cache))
    bridge.save_model_map({"normal": "gemini-9-flash"}, ["gemini-9-flash"])
    assert bridge.model_map() == {"normal": "gemini-9-flash"}
    assert json.loads(cache.read_text())["transport"] == "gemini"


def test_backend_registration_preserves_tier_defaults():
    config = default_config()
    models = {"fast": "a", "normal": "b", "smart": "c", "max": "d"}
    assert ensure_backend(config, 8817, models) is True
    backend = config.backends["gemini"]
    assert backend.codex_provider == "agentroute-gemini"
    assert backend.base_url == "http://127.0.0.1:8817/v1"
    assert backend.tiers["fast"].model == "a"
    assert backend.review_model == "a"
    assert config.routing.backend_by_tier["fast"] == "gpt"
    assert config.capabilities.models["a"].tool_calling == "full"


def test_backend_registration_updates_an_existing_backend():
    config = default_config()
    ensure_backend(config, 8817, {"normal": "b"})
    ensure_backend(config, 8818, {"normal": "b2"})
    assert config.backends["gemini"].base_url == "http://127.0.0.1:8818/v1"
    assert config.backends["gemini"].tiers["normal"].model == "b2"


def test_credential_requires_login(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_GEMINI_CREDENTIALS", str(tmp_path / "missing.json"))
    with pytest.raises(bridge.CredentialError, match="agentroute gemini login"):
        bridge.GeminiCredential().token()


def test_credential_reports_missing_fields(tmp_path, monkeypatch):
    path = tmp_path / "cred.json"
    path.write_text(json.dumps({"client_id": "x"}))
    monkeypatch.setenv("AGENTROUTE_GEMINI_CREDENTIALS", str(path))
    with pytest.raises(bridge.CredentialError, match="client_secret"):
        bridge.GeminiCredential().token()


def test_credential_refreshes_and_persists_rotation(tmp_path, monkeypatch):
    path = tmp_path / "cred.json"
    path.write_text(
        json.dumps({"client_id": "cid", "client_secret": "csec", "refresh_token": "old"})
    )
    monkeypatch.setenv("AGENTROUTE_GEMINI_CREDENTIALS", str(path))

    class Response:
        status = 200

        def read(self):
            return json.dumps(
                {"access_token": "fresh", "expires_in": 3600, "refresh_token": "new"}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(bridge.urllib.request, "urlopen", lambda *a, **k: Response())
    assert bridge.GeminiCredential().token() == "fresh"
    assert json.loads(path.read_text())["refresh_token"] == "new"


def test_credential_check_proves_the_api_accepts_it(tmp_path, monkeypatch):
    path = tmp_path / "cred.json"
    path.write_text(json.dumps({"client_id": "cid", "client_secret": "csec", "refresh_token": "r"}))
    monkeypatch.setenv("AGENTROUTE_GEMINI_CREDENTIALS", str(path))
    credential = bridge.GeminiCredential()
    monkeypatch.setattr(credential, "token", lambda: "tok")
    seen = {}

    def fake_list(token):
        seen["token"] = token
        return ["models/gemini-2.5-pro"]

    monkeypatch.setattr(bridge, "list_models", fake_list)
    monkeypatch.setattr(bridge, "grant_scopes", lambda token: f"{bridge.GENERATIVE_SCOPE} openid")
    assert credential.check() == [bridge.GENERATIVE_SCOPE, "openid"]
    assert seen["token"] == "tok"


def test_models_listing_filters_non_chat_methods(monkeypatch):
    class Response:
        def read(self):
            return json.dumps(
                {
                    "models": [
                        {
                            "name": "models/gemini-2.5-pro",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            "name": "models/text-embedding-004",
                            "supportedGenerationMethods": ["embedContent"],
                        },
                    ]
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(bridge, "_authorized", lambda *a, **k: Response())
    assert bridge.list_models("tok") == ["models/gemini-2.5-pro"]


def _mock_gemini(status: int, frames: list[dict]):
    """Serve a Gemini-shaped SSE stream from a loopback port."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler contract
            length = int(self.headers.get("content-length") or 0)
            self.rfile.read(length)
            payload = "".join(
                "data: " + json.dumps(frame) + "\r\n\r\n" for frame in frames
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/v1beta"


def _bridge_client(monkeypatch, api_base: str, tmp_path):
    """Run the real bridge handler against a recorded Gemini endpoint."""
    import threading

    path = tmp_path / "cred.json"
    path.write_text(json.dumps({"client_id": "cid", "client_secret": "sec", "refresh_token": "r"}))
    monkeypatch.setenv("AGENTROUTE_GEMINI_CREDENTIALS", str(path))
    monkeypatch.setenv("AGENTROUTE_GEMINI_API_BASE", api_base)
    monkeypatch.setattr(bridge.GeminiCredential, "token", lambda self: "token")

    server = bridge.GeminiServer(("127.0.0.1", 0), bridge.GeminiHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/v1/responses"


def test_bridge_streams_a_native_tool_call_over_http(monkeypatch, tmp_path):
    import urllib.request

    frames = [
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"functionCall": {"name": "shell", "args": {"command": "cat a.txt"}}}
                        ]
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 21, "candidatesTokenCount": 6},
        }
    ]
    gemini, base = _mock_gemini(200, frames)
    proxy, url = _bridge_client(monkeypatch, base, tmp_path)
    try:
        body = json.dumps(
            {
                "model": MODEL,
                "stream": True,
                "input": [
                    {"role": "user", "content": [{"type": "input_text", "text": "cat a.txt"}]}
                ],
                "tools": [FUNCTION_TOOL],
            }
        ).encode()
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            events = response.read().decode()
    finally:
        proxy.shutdown()
        gemini.shutdown()
    assert "response.created" in events
    assert "response.function_call_arguments.done" in events
    completed = [
        block for block in events.split("\n\n") if block.startswith("event: response.completed")
    ][0]
    payload = json.loads(completed.split("data: ", 1)[1])
    item = payload["response"]["output"][0]
    assert item["type"] == "function_call"
    assert item["name"] == "shell"
    assert json.loads(item["arguments"]) == {"command": "cat a.txt"}
    assert payload["response"]["usage"]["input_tokens"] == 21


def test_bridge_surfaces_an_upstream_error(monkeypatch, tmp_path):
    import urllib.error
    import urllib.request

    gemini, base = _mock_gemini(403, [{"error": {"message": "API key not valid"}}])
    proxy, url = _bridge_client(monkeypatch, base, tmp_path)
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps({"model": MODEL, "input": "hi"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=30)
        assert caught.value.code in (403, 502)
    finally:
        proxy.shutdown()
        gemini.shutdown()


def test_healthz_reports_transport_and_tools(monkeypatch):
    import urllib.request

    class Credential:
        def check(self):
            return [bridge.GENERATIVE_SCOPE]

    monkeypatch.setattr(
        bridge.GeminiHandler, "credential_factory", staticmethod(lambda: Credential())
    )
    monkeypatch.setattr(bridge.GeminiHandler, "log_message", lambda *a: None)
    import threading

    server = bridge.GeminiServer(("127.0.0.1", 0), bridge.GeminiHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/healthz", timeout=10
        ) as response:
            health = json.loads(response.read().decode())
    finally:
        server.shutdown()
    assert health["credential_status"] == "ok"
    assert health["tool_calling"] == "full"
    assert health["tool_calling_mode"] == "native_function_calling"
    assert health["transport"] == "gemini"


def test_cli_mounts_the_gemini_command():
    from typer.testing import CliRunner

    from agentroute.cli import app

    result = CliRunner().invoke(app, ["gemini", "--help"])
    assert result.exit_code == 0
    assert "login" in result.stdout


def test_cli_has_no_gemini_import_cycles():
    import agentroute.cli  # noqa: F401
    import agentroute.gemini_cli  # noqa: F401

    assert callable(agentroute.gemini_cli.install_command)


def test_install_registers_even_without_a_credential(monkeypatch, tmp_path):
    """Before the one-time consent, install still leaves a runnable service."""
    from typer.testing import CliRunner

    import agentroute.gemini_cli as cli

    monkeypatch.setenv("AGENTROUTE_GEMINI_CREDENTIALS", str(tmp_path / "missing.json"))
    monkeypatch.setenv("AGENTROUTE_GEMINI_MODELS_CACHE", str(tmp_path / "models.json"))
    monkeypatch.setattr(cli, "load_config", lambda: default_config())
    saved = {}
    monkeypatch.setattr(cli, "save_config", lambda config: saved.update(config=config))
    monkeypatch.setattr(cli, "sync_codex_providers", lambda config: (tmp_path, None))
    result = CliRunner().invoke(cli.app, ["install", "--no-service"])
    assert result.exit_code == 0
    assert "not usable yet" in result.stdout
    assert saved["config"].backends["gemini"].codex_provider == "agentroute-gemini"


def test_desktop_client_json_is_accepted(tmp_path):
    import typer as _typer

    import agentroute.gemini_cli as cli

    path = tmp_path / "client.json"
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "cid.apps.googleusercontent.com",
                    "client_secret": "GOCSPX-secret",
                    "redirect_uris": ["http://localhost"],
                }
            }
        )
    )
    assert cli.client_from_secrets_file(path) == ("cid.apps.googleusercontent.com", "GOCSPX-secret")
    path.write_text(json.dumps({"web": {"client_id": "x", "client_secret": "y"}}))
    with pytest.raises(_typer.BadParameter, match="Desktop OAuth client"):
        cli.client_from_secrets_file(path)


def test_default_client_json_path_can_be_overridden(monkeypatch, tmp_path):
    import agentroute.gemini_cli as cli

    monkeypatch.setenv("AGENTROUTE_GEMINI_CLIENT_SECRETS", str(tmp_path / "c.json"))
    assert cli.default_client_secrets_path() == tmp_path / "c.json"


def test_price_family_covers_legacy_and_current_models():
    from agentroute.gemini_bridge import price_family

    assert price_family("gemini-2.5-pro") == "gemini-2.5-pro"
    assert price_family("models/gemini-3.8-flash") == "gemini-flash"
    assert price_family("gemini-3.5-flash-lite") == "gemini-flash-lite"
    assert price_family("gemini-4-carbon") == "gemini-pro"
    assert price_family("gemini-4-argon") == "gemini-pro"


def test_every_tier_model_is_priced_after_install():
    from agentroute.gemini_bridge import price_family

    config = default_config()
    models = {
        "fast": "gemini-3.5-flash-lite",
        "normal": "gemini-3.8-flash",
        "smart": "gemini-4-argon",
        "max": "gemini-4-carbon",
    }
    ensure_backend(config, 8817, models)
    for model in models.values():
        assert config.pricing.families[model] == price_family(model)
        assert model not in config.pricing.aliases
        assert config.capabilities.models[model].pricing_model == price_family(model)
    from agentroute.pricing import canonical_model

    for model in models.values():
        assert canonical_model(model, config.pricing) in config.pricing.models


def test_usage_totals_price_recorded_gemini_turns():
    import datetime

    import agentroute.gemini_cli as cli

    config = default_config()
    ensure_backend(config, 8817, {"normal": "gemini-3.8-flash"})
    now = datetime.datetime(2026, 11, 26, 12, 0, tzinfo=datetime.timezone.utc)
    row = {
        "usage_recorded_at": now.isoformat(),
        "created_at": now.isoformat(),
        "backend": "gemini",
        "answer_model": "gemini-3.8-flash",
        "model": "gemini-3.8-flash",
        "answer_input_tokens": 1_000_000,
        "answer_cached_input_tokens": 0,
        "answer_cache_write_input_tokens": 0,
        "answer_output_tokens": 1_000_000,
        "execution_receipt": "{}",
    }
    other = dict(row, backend="gpt", answer_model="gpt-6.2-sol")
    totals = cli.usage_totals([row, other], config, now=now)
    assert totals["today"]["turns"] == 1
    assert totals["today"]["input_tokens"] == 1_000_000
    # $0.75 input + $3.75 output for one million tokens each.
    assert totals["today"]["cost_usd"] == pytest.approx(4.50)
    assert totals["month"]["cost_usd"] == pytest.approx(4.50)
    assert totals["unpriced"] == []


def test_usage_totals_flag_unpriced_models():
    import datetime

    import agentroute.gemini_cli as cli

    config = default_config()
    now = datetime.datetime(2026, 11, 26, 12, 0, tzinfo=datetime.timezone.utc)
    row = {
        "usage_recorded_at": now.isoformat(),
        "created_at": now.isoformat(),
        "backend": "gemini",
        "answer_model": "gemini-9-experimental",
        "model": "gemini-9-experimental",
        "answer_input_tokens": 100,
        "answer_cached_input_tokens": 0,
        "answer_cache_write_input_tokens": 0,
        "answer_output_tokens": 100,
        "execution_receipt": "{}",
    }
    totals = cli.usage_totals([row], config, now=now)
    assert totals["unpriced"] == ["gemini-9-experimental"]
    assert totals["today"]["cost_usd"] == 0


def test_scheduled_price_change_switches_on_its_date():
    import datetime

    from agentroute.config import PriceChange
    from agentroute.pricing import rate_on

    rate = default_config().pricing.models["gemini-pro"]
    assert rate.input_per_million == 2.00
    before = rate_on(rate, datetime.date(2026, 11, 30))
    after = rate_on(rate, datetime.date(2026, 12, 1))
    assert (before.input_per_million, before.output_per_million) == (2.00, 10.00)
    assert (after.input_per_million, after.output_per_million) == (4.00, 20.00)
    assert after.cached_input_per_million == 0.20
    assert rate.scheduled[0] == PriceChange(
        effective_from=datetime.date(2026, 12, 1),
        input_per_million=4.00,
        cached_input_per_million=0.20,
        output_per_million=20.00,
    )


def test_turn_cost_uses_the_rate_of_the_recorded_day():

    from agentroute.pricing import turn_cost

    config = default_config()
    config.pricing.aliases["gemini-4-carbon"] = "gemini-pro"

    def row(day: str):
        return {
            "created_at": f"{day}T12:00:00+00:00",
            "answer_input_tokens": 1_000_000,
            "answer_cached_input_tokens": 0,
            "answer_cache_write_input_tokens": 0,
            "answer_output_tokens": 0,
            "execution_receipt": "{}",
        }

    assert turn_cost(row("2026-11-30"), "gemini-4-carbon", config.pricing) == pytest.approx(2.00)
    assert turn_cost(row("2026-12-01"), "gemini-4-carbon", config.pricing) == pytest.approx(4.00)


def test_retired_backend_is_removed_with_its_references():
    from agentroute.config import retire_backends

    config = default_config()
    config.backends["antigravity"] = config.backends["gpt"].model_copy(deep=True)
    config.backends["antigravity"].codex_provider = "agentroute-antigravity"
    config.backends["antigravity"].tiers = {
        "normal": config.backends["gpt"]
        .tiers["normal"]
        .model_copy(update={"model": "gemini-3.8-flash-medium"})
    }
    config.capabilities.models["gemini-3.8-flash-medium"] = ModelCapabilities(tool_calling="full")
    config.routing.backend_by_tier["normal"] = "antigravity"
    config.routing.progress_review_backend = "antigravity"
    config.capacity.default_fallback_backend = "antigravity"

    assert retire_backends(config) == ["antigravity"]
    assert "antigravity" not in config.backends
    assert "gemini-3.8-flash-medium" not in config.capabilities.models
    assert config.routing.backend_by_tier["normal"] == "gpt"
    assert config.routing.progress_review_backend is None
    assert config.capacity.default_fallback_backend is None
    assert retire_backends(config) == []


def test_install_does_not_create_catalog_aliases_for_new_models():
    """A pricing family must never become a Codex catalog alias.

    Catalog aliases copy metadata from a real catalog model, so pointing one at
    ``gemini-flash`` made `sync_codex_providers` fail with "catalog has no
    metadata". Unknown Gemini IDs are synthesized from their declared limits.
    """
    from agentroute.providers import _write_model_catalog  # noqa: F401

    config = default_config()
    ensure_backend(config, 8817, {"normal": "gemini-3.8-flash"})
    assert "gemini-3.8-flash" not in config.pricing.aliases
    assert config.pricing.families["gemini-3.8-flash"] == "gemini-flash"


def test_install_repairs_a_config_polluted_by_family_aliases():
    """A config written by the earlier build must not keep failing to sync."""
    config = default_config()
    config.pricing.aliases["gemini-2.5-flash"] = "gemini-flash"
    config.pricing.families.pop("gemini-2.5-flash", None)
    ensure_backend(config, 8817, {"normal": "gemini-3.8-flash"})
    assert "gemini-2.5-flash" not in config.pricing.aliases
    assert config.pricing.families["gemini-3.8-flash"] == "gemini-flash"
