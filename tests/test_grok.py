import json
import subprocess
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest

from agentroute.config import default_config
from agentroute.grok_bridge import (
    PATCH_FUNCTION,
    GrokCredential,
    ensure_backend,
    request_headers,
    translate_request,
    translate_stream,
    wire_name,
)
from agentroute.grok_cli import service_definition


def login(tmp_path, seconds=3600, token="secret"):
    record = {
        "key": token,
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
    }
    (tmp_path / "auth.json").write_text(json.dumps({"https://auth.x.ai::client": record}))


def test_credential_reads_current_file_without_exporting_or_caching(tmp_path, monkeypatch):
    monkeypatch.delenv("GROK_AUTH_PATH", raising=False)
    login(tmp_path)
    credential = GrokCredential("grok", tmp_path)
    assert credential.token() == "secret"
    login(tmp_path, token="rotated")
    assert credential.token() == "rotated"
    assert request_headers(credential, "1.0.70", "grok-4.7")["x-grok-model-override"] == "grok-4.7"


def test_refresh_delegates_to_cli_and_rereads_rotation(tmp_path, monkeypatch):
    monkeypatch.delenv("GROK_AUTH_PATH", raising=False)
    login(tmp_path, -1)

    def refresh(args, **kwargs):
        assert args == ["/grok", "models"]
        assert kwargs["stdout"] == subprocess.DEVNULL
        login(tmp_path, token="new")
        return type("Refresh", (), {"wait": lambda self, **kw: 0})()

    monkeypatch.setattr(subprocess, "Popen", refresh)
    assert GrokCredential("/grok", tmp_path).token() == "new"


def test_expired_refresh_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("GROK_AUTH_PATH", raising=False)
    login(tmp_path, -1)
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: type("Refresh", (), {"wait": lambda self, **kw: 0})()
    )
    with pytest.raises(ValueError, match="expired"):
        GrokCredential("grok", tmp_path).token()


def test_backend_install_preserves_routes_and_custom_tiers():
    config = default_config()
    before = dict(config.routing.backend_by_tier)
    ensure_backend(config)
    assert config.routing.backend_by_tier == before
    config.backends["grok"].tiers["fast"].model = "grok-4.5"
    ensure_backend(config, 9000)
    assert config.backends["grok"].tiers["fast"].model == "grok-4.5"
    assert config.backends["grok"].base_url == "http://127.0.0.1:9000/v1"
    assert config.capabilities.models["grok-4.7"].context_window == 256000


def test_patch_roundtrip_and_reasoning_passthrough():
    patch = "*** Begin Patch\n*** End Patch"
    request = {
        "tools": [{"type": "custom", "name": "apply_patch"}],
        "input": [
            {"type": "custom_tool_call", "name": "apply_patch", "input": patch, "call_id": "c"},
            {"type": "custom_tool_call_output", "call_id": "c", "output": "ok"},
            {"type": "reasoning", "summary": [], "content": None, "encrypted_content": "opaque"},
        ],
    }
    converted = translate_request(request)
    assert converted["tools"][0]["name"] == PATCH_FUNCTION
    assert converted["input"][0]["arguments"] == json.dumps({"input": patch})
    assert converted["input"][1]["type"] == "function_call_output"
    assert "content" not in converted["input"][2]
    assert converted["input"][2]["summary"] == []
    assert request["tools"][0]["type"] == "custom"
    item = {"type": "function_call", "name": PATCH_FUNCTION, "id": "i", "arguments": ""}
    events = [
        {"type": "response.output_item.added", "item": item},
        {"type": "response.function_call_arguments.delta", "item_id": "i", "delta": "{"},
        {
            "type": "response.function_call_arguments.done",
            "item_id": "i",
            "arguments": json.dumps({"input": patch}),
        },
        {
            "type": "response.output_item.done",
            "item": dict(item, arguments=json.dumps({"input": patch})),
        },
        {"type": "response.completed", "response": {"output": [converted["input"][2]]}},
    ]
    stream = BytesIO(b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events))
    result = b"".join(translate_stream(stream))
    parsed = [json.loads(row[6:]) for row in result.splitlines() if row.startswith(b"data: ")]
    assert parsed[0]["item"]["type"] == "custom_tool_call"
    assert parsed[1]["delta"] == patch
    assert parsed[2]["item"]["input"] == patch
    assert parsed[3]["response"]["output"][0]["encrypted_content"] == "opaque"


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_services_are_independent_of_claude(monkeypatch, system):
    monkeypatch.setattr("agentroute.grok_cli.platform.system", lambda: system)
    monkeypatch.setattr("agentroute.grok_cli._agentroute_executable", lambda: "/agentroute")
    definition = service_definition("/usr/bin/grok", 8092)
    assert b"grok" in definition and b"claude" not in definition


def test_credential_rejects_other_issuers(tmp_path, monkeypatch):
    monkeypatch.delenv("GROK_AUTH_PATH", raising=False)
    (tmp_path / "auth.json").write_text(json.dumps({"https://enterprise::id": {"key": "secret"}}))
    with pytest.raises(ValueError):
        GrokCredential("grok", tmp_path).token()


def test_namespaces_are_flattened_and_openai_metadata_removed():
    body = translate_request(
        {
            "model": "grok-4.7",
            "service_tier": "priority",
            "tools": [
                {
                    "type": "namespace",
                    "name": "functions",
                    "tools": [{"type": "function", "name": "read", "parameters": {}}],
                }
            ],
        }
    )
    assert "service_tier" not in body
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["name"] == wire_name("functions", "read")


def test_compaction_without_tools_removes_tool_controls():
    body = translate_request(
        {"model": "grok-4.7", "tools": [], "tool_choice": "auto", "parallel_tool_calls": True}
    )
    assert body["tools"] == []
    assert "tool_choice" not in body and "parallel_tool_calls" not in body


def test_http_streams_and_rejects_browser_origin(monkeypatch):
    import threading
    import urllib.error
    import urllib.request

    from agentroute.grok_bridge import GrokServer

    credential = type(
        "Credential", (), {"version": lambda self: "1.0.70", "token": lambda self: "token"}
    )()
    server = GrokServer(0, credential)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    address = f"http://127.0.0.1:{server.server_port}"
    try:
        request = urllib.request.Request(
            address + "/v1/responses", data=b"{}", headers={"Origin": "https://example.com"}
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 403
        assert json.load(urllib.request.urlopen(address + "/healthz"))["provider"] == "grok"
    finally:
        server.shutdown()
        server.server_close()


def test_namespace_collisions_keep_distinct_tools_and_history():
    from agentroute.grok_bridge import patch_item, tool_names

    request = {
        "tools": [
            {
                "type": "namespace",
                "name": namespace,
                "tools": [{"type": "function", "name": "read", "parameters": {"type": "object"}}],
            }
            for namespace in ("first", "second")
        ],
        "input": [
            {
                "type": "function_call",
                "namespace": "second",
                "name": "read",
                "call_id": "c",
                "arguments": "{}",
            }
        ],
    }
    body = translate_request(request)
    assert len({t["name"] for t in body["tools"]}) == 2
    assert body["input"][0]["name"] == body["tools"][1]["name"]
    item = {"type": "function_call", "name": body["tools"][1]["name"]}
    assert patch_item(item, tool_names(request))["namespace"] == "second"
    assert item["name"] == "read"


def test_grok_billing_capacity_account_binding_and_staleness(tmp_path, monkeypatch):
    import time

    from agentroute.capacity import backend_state
    from agentroute.grok_usage import parse_billing, write_snapshot

    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path / "route"))
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    monkeypatch.delenv("GROK_AUTH_PATH", raising=False)
    login(tmp_path)
    credential = GrokCredential("grok", tmp_path)
    config = default_config()
    config.capacity.enabled = True
    ensure_backend(config)
    snapshot = parse_billing(
        {"config": {"creditUsagePercent": 90, "currentPeriod": {"end": "2099-01-01T00:00:00Z"}}}
    )
    write_snapshot(credential, snapshot)
    state = backend_state(config, "grok", probe=False)
    assert state.status == "warning" and state.used_percent == 90
    assert state.backend == "grok" and state.resets_at is not None
    assert backend_state(config, "grok", recovery_hold=True, probe=False).status == "exhausted"
    write_snapshot(credential, {"used_percent": 100})
    assert not backend_state(config, "grok", probe=False).available
    write_snapshot(credential, {"blocked_until": time.time() + 60})
    assert backend_state(config, "grok", probe=False).status == "unavailable"
    login(tmp_path, token="another-account")
    assert backend_state(config, "grok", probe=False).status == "unknown"


def test_billing_schema_and_invalid_measurements():
    from agentroute.grok_usage import parse_billing

    assert (
        parse_billing({"config": {"monthlyLimit": {"val": 100}, "used": {"val": 25}}})[
            "used_percent"
        ]
        == 25
    )
    assert parse_billing({"config": {}})["used_percent"] is None
    for value in (float("nan"), float("inf"), -1):
        with pytest.raises(ValueError):
            parse_billing({"config": {"creditUsagePercent": value}})


def test_grok_documented_price_rates_and_long_context():
    from agentroute.pricing import token_cost

    pricing = default_config().pricing
    usage = {"input_tokens": 100_000, "cached_input_tokens": 50_000, "output_tokens": 10_000}
    assert token_cost("grok-4.7", usage, pricing) == pytest.approx(0.185)
    assert token_cost("grok-4.7-build-fast", usage, pricing) == pytest.approx(0.37)
    assert token_cost("grok-4.7-build-fast", {"input_tokens": 200_001}, pricing) == pytest.approx(
        200_001 * 6 / 1_000_000
    )


def test_http_tool_stream_and_rate_limit_cooldown(tmp_path, monkeypatch):
    import threading
    import urllib.error
    import urllib.request
    from email.message import Message

    import agentroute.grok_bridge as bridge
    from agentroute.grok_usage import read_snapshot

    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path / "route"))
    monkeypatch.delenv("GROK_AUTH_PATH", raising=False)
    login(tmp_path)
    credential = GrokCredential("grok", tmp_path)
    monkeypatch.setattr(credential, "version", lambda: "1.0.70")
    seen = []
    limited = False

    def upstream(request, *, timeout):
        assert request.headers["Authorization"] == "Bearer secret"
        seen.append(json.loads(request.data))
        headers = Message()
        if limited:
            headers["Retry-After"] = "300"
            raise urllib.error.HTTPError(
                request.full_url, 429, "limited", headers, BytesIO(b"sensitive upstream diagnostic")
            )
        headers["Content-Type"] = "text/event-stream"
        response = BytesIO(b'data: {"type":"response.completed","response":{"output":[]}}\n\n')
        response.headers = headers
        return response

    monkeypatch.setattr(bridge, "open_upstream", upstream)
    server = bridge.GrokServer(0, credential)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://127.0.0.1:{server.server_port}"

    def request():
        return urllib.request.Request(
            address + "/v1/responses", data=json.dumps({"model": "grok-4.7"}).encode()
        )

    try:
        with urllib.request.urlopen(request()) as response:
            assert b"response.completed" in response.read()
        assert seen[0]["store"] is False and seen[0]["stream"] is True
        limited = True
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request())
        assert error.value.code == 429
        assert b"sensitive" not in error.value.read()
        assert read_snapshot(credential)["blocked_until"] > datetime.now(timezone.utc).timestamp()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_stale_failed_probe_does_not_relabel_old_success(tmp_path, monkeypatch):
    import time
    import urllib.request

    from agentroute.grok_usage import capacity_snapshot, read_snapshot, write_snapshot

    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path / "route"))
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    monkeypatch.delenv("GROK_AUTH_PATH", raising=False)
    login(tmp_path)
    credential = GrokCredential("grok", tmp_path)
    snapshot = write_snapshot(credential, {"used_percent": 25})
    path = tmp_path / "route/grok-usage.json"
    snapshot["observed_at"] = time.time() - 70
    path.write_text(json.dumps(snapshot))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: BytesIO(b'{"error":"failed"}'))
    assert capacity_snapshot("http://127.0.0.1:8092/v1") == {}
    assert capacity_snapshot("http://127.0.0.1:8092/v1", offline=True)["used_percent"] == 25
    snapshot["observed_at"] = time.time() - 200
    path.write_text(json.dumps(snapshot))
    assert capacity_snapshot("http://127.0.0.1:8092/v1", offline=True) == {}
    snapshot["blocked_until"] = time.time() + 60
    path.write_text(json.dumps(snapshot))
    assert (
        capacity_snapshot("http://127.0.0.1:8092/v1", offline=True)["blocked_until"] > time.time()
    )
    path.write_text("[]")
    assert read_snapshot(credential) == {}


def test_grok_quota_fallback_and_explicit_lock(monkeypatch):
    from agentroute.models import RouteContext
    from agentroute.router import Router

    monkeypatch.setattr(
        "agentroute.grok_usage.capacity_snapshot", lambda *a, **k: {"used_percent": 100}
    )
    config = default_config()
    config.capacity.enabled = True
    ensure_backend(config)
    config.routing.backend_by_tier = {tier: "grok" for tier in ("fast", "normal", "smart", "max")}
    config.backends["grok"].fallback_backend = "gpt"
    automatic = Router(config).route(RouteContext(session_id="grok-quota", latest_prompt="hello"))
    assert automatic.backend == "gpt" and automatic.capacity_status == "fallback"
    locked = Router(config).route(
        RouteContext(session_id="grok-quota", latest_prompt="@grok hello")
    )
    assert locked.backend == "grok" and locked.capacity_blocked
