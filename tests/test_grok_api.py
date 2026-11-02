import json
import os
import threading
import urllib.error
import urllib.request
from email.message import Message
from io import BytesIO
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentroute.config import default_config
from agentroute.grok_bridge import (
    API_UPSTREAM,
    BRIDGE_KEY_ENV,
    GrokApiCredential,
    GrokCredential,
    GrokServer,
    ensure_backend,
)
from agentroute.grok_cli import app
from agentroute.grok_usage import capacity_snapshot, fetch_usage, read_snapshot


@pytest.fixture
def api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    path = tmp_path / "credentials.env"
    path.write_text(
        "export OTHER_KEY=preserve\nexport XAI_API_KEY='api-secret'\n"
        f"export {BRIDGE_KEY_ENV}=ar-grok-local-secret\n"
    )
    path.chmod(0o600)
    monkeypatch.setenv("AGENTROUTE_CREDENTIALS_FILE", str(path))
    upstream = tmp_path / "upstream.env"
    upstream.write_text("export XAI_API_KEY=api-secret\n")
    upstream.chmod(0o600)
    monkeypatch.setenv("AGENTROUTE_GROK_API_CREDENTIALS_FILE", str(upstream))
    return path


def test_api_credential_reads_rotation_without_cli_or_shell(api_key, monkeypatch):
    upstream = Path(os.environ["AGENTROUTE_GROK_API_CREDENTIALS_FILE"])
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("No CLI or shell"))
    source = GrokApiCredential()
    assert source.token() == "api-secret"
    upstream.write_text("export XAI_API_KEY='rotated-$(do-not-execute)'\n")
    assert source.token() == "rotated-$(do-not-execute)"
    assert source.version() is None


@pytest.mark.parametrize("bad_file", ["permissions", "duplicate", "missing", "newline", "symlink"])
def test_api_credential_rejects_unsafe_store(api_key, bad_file):
    api_key = Path(os.environ["AGENTROUTE_GROK_API_CREDENTIALS_FILE"])
    if bad_file == "permissions":
        api_key.chmod(0o644)
    elif bad_file == "duplicate":
        api_key.write_text("export XAI_API_KEY=one\nexport XAI_API_KEY=two\n")
    elif bad_file == "missing":
        api_key.write_text("export OTHER_KEY=secret\n")
    elif bad_file == "newline":
        api_key.write_text("export XAI_API_KEY='bad\nheader'\n")
    else:
        target = api_key.with_suffix(".target")
        api_key.rename(target)
        api_key.symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        GrokApiCredential().token()


def test_api_backend_preserves_routes_and_existing_context():
    config = default_config()
    routes = config.routing.model_dump()
    ensure_backend(config, credential_mode="api-key")
    assert config.routing.model_dump() == routes
    assert {target.model for target in config.backends["grok"].tiers.values()} == {"grok-4.7"}
    assert config.capabilities.models["grok-4.7"].context_window == 500000
    config.backends["grok"].api_key_env = "XAI_API_KEY"
    config.backends["grok"].tiers["fast"].model = "grok-4.5"
    config.capabilities.models["grok-4.7"].context_window = 100000
    ensure_backend(config, 9092, "api-key")
    assert config.backends["grok"].api_key_env == BRIDGE_KEY_ENV
    assert config.backends["grok"].tiers["fast"].model == "grok-4.5"
    assert config.capabilities.models["grok-4.7"].context_window == 100000


def test_api_credential_import_uses_existing_store(api_key, monkeypatch):
    monkeypatch.setenv("SOURCE_KEY", "new-secret")
    result = CliRunner().invoke(app, ["credential-import", "SOURCE_KEY"])
    assert result.exit_code == 0
    assert "new-secret" not in result.output
    assert "OTHER_KEY=preserve" in api_key.read_text()
    assert GrokApiCredential().token() == "new-secret"
    assert api_key.stat().st_mode & 0o777 == 0o600


def test_api_key_import_is_separate_from_client_credential_store(api_key, tmp_path, monkeypatch):
    from agentroute.providers import stored_credential_names

    upstream_path = tmp_path / "upstream.env"
    monkeypatch.setenv("AGENTROUTE_GROK_API_CREDENTIALS_FILE", str(upstream_path))
    monkeypatch.setenv("SOURCE_KEY", "private-api-secret")
    before = api_key.read_bytes()
    result = CliRunner().invoke(app, ["credential-import", "SOURCE_KEY"])
    assert result.exit_code == 0
    assert api_key.read_bytes() == before
    assert stored_credential_names(upstream_path) == {"XAI_API_KEY"}
    assert GrokApiCredential().token() == "private-api-secret"
    assert GrokApiCredential().client_key() == "ar-grok-local-secret"


def test_api_import_rejects_client_credential_file_collision(api_key, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_GROK_API_CREDENTIALS_FILE", str(api_key))
    monkeypatch.setenv("SOURCE_KEY", "private-api-secret")
    before = api_key.read_bytes()
    result = CliRunner().invoke(app, ["credential-import", "SOURCE_KEY"])
    assert result.exit_code != 0
    assert api_key.read_bytes() == before


def test_subscription_rejects_loaded_api_client_before_inference(api_key, monkeypatch):
    monkeypatch.setattr(
        "agentroute.grok_bridge.open_upstream", lambda *a, **k: pytest.fail("No upstream request")
    )
    credential = type("Subscription", (), {"version": lambda self: "1.0.70"})()
    server = GrokServer(0, credential)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/responses",
            data=b'{"model":"grok-4.7"}',
            headers={"Authorization": "Bearer ar-grok-local-secret"},
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_api_billing_never_contacts_subscription_proxy(api_key, monkeypatch):
    monkeypatch.setattr(
        "agentroute.grok_bridge.open_upstream", lambda *a, **k: pytest.fail("No billing request")
    )
    assert fetch_usage(GrokApiCredential(), None) == {"error": "API billing telemetry unavailable"}


def test_switch_to_api_migrates_only_subscription_only_model():
    config = default_config()
    ensure_backend(config)
    config.backends["grok"].tiers["smart"].model = "grok-4.5"
    ensure_backend(config, credential_mode="api-key")
    assert config.backends["grok"].tiers["fast"].model == "grok-4.7"
    assert config.backends["grok"].review_model == "grok-4.7"
    assert config.backends["grok"].tiers["smart"].model == "grok-4.5"


def test_shipped_subscription_tiers_migrate_to_api_defaults():
    config = default_config()
    ensure_backend(config)
    ensure_backend(config, credential_mode="api-key")
    assert {target.model for target in config.backends["grok"].tiers.values()} == {"grok-4.7"}
    assert config.backends["grok"].display_name == "Grok API"
    ensure_backend(config)
    assert config.backends["grok"].display_name == "Grok subscription (Grok CLI)"


def test_patch_tool_choice_is_translated_and_original_untouched():
    from agentroute.grok_bridge import translate_request

    body = {
        "tools": [{"type": "custom", "name": "apply_patch"}],
        "tool_choice": {"type": "custom", "name": "apply_patch"},
    }
    translated = translate_request(body)
    assert translated["tool_choice"] == {"type": "function", "name": "agentroute_apply_patch"}
    assert body["tool_choice"]["type"] == "custom"


def test_mode_switch_does_not_reuse_other_mode_cooldown(api_key, tmp_path, monkeypatch):
    import time
    from datetime import datetime, timedelta, timezone

    from agentroute.grok_usage import write_snapshot

    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    monkeypatch.delenv("GROK_AUTH_PATH", raising=False)
    (tmp_path / "auth.json").write_text(
        json.dumps({
            "https://auth.x.ai::id": {
                "key": "subscription-secret",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            }
        })
    )
    write_snapshot(GrokCredential("grok"), {"blocked_until": time.time() + 120})
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda url, **k: BytesIO(
            b'{"provider":"grok","credential_mode":"api-key"}'
            if url.endswith("/healthz")
            else b'{"credential_mode":"api-key","billing":"unavailable"}'
        ),
    )
    assert capacity_snapshot("http://127.0.0.1:8092/v1") == {}
    # Offline inspection can only report its explicitly cached credential mode.
    cached = capacity_snapshot("http://127.0.0.1:8092/v1", offline=True)
    assert cached["blocked_until"] > time.time()


def test_late_old_key_cooldown_cannot_replace_current_key_sample(api_key):
    from agentroute.grok_usage import account_key, write_snapshot

    credential = GrokApiCredential()
    old_account = account_key(credential)
    upstream = Path(os.environ["AGENTROUTE_GROK_API_CREDENTIALS_FILE"])
    upstream.write_text("export XAI_API_KEY=new-api-secret\n")
    current = write_snapshot(credential, {"blocked_until": 9999999999})
    write_snapshot(credential, {"blocked_until": 1}, account=old_account)
    assert read_snapshot(credential) == current


def test_api_capacity_does_not_probe_non_loopback_origin(api_key, monkeypatch):
    from agentroute.grok_usage import write_snapshot

    write_snapshot(GrokApiCredential(), {})
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("No remote probe"))
    capacity_snapshot("https://api.x.ai/v1")


def test_cooldown_writers_are_serialized_across_key_rotation(api_key, monkeypatch):
    from agentroute.grok_usage import account_key, write_snapshot

    credential = GrokApiCredential()
    old_account = account_key(credential)
    original = credential._read
    checked = threading.Event()
    release = threading.Event()
    current_done = threading.Event()

    def read():
        snapshot = original()
        if threading.current_thread().name == "old-key-writer":
            checked.set()
            assert release.wait(5)
        return snapshot

    monkeypatch.setattr(credential, "_read", read)
    old = threading.Thread(
        name="old-key-writer",
        target=lambda: write_snapshot(credential, {"blocked_until": 1}, account=old_account),
    )
    old.start()
    assert checked.wait(5)
    upstream = Path(os.environ["AGENTROUTE_GROK_API_CREDENTIALS_FILE"])
    upstream.write_text("export XAI_API_KEY=new-api-secret\n")

    def current_write():
        write_snapshot(credential, {"blocked_until": 9999999999})
        current_done.set()

    current = threading.Thread(target=current_write)
    current.start()
    try:
        assert not current_done.wait(0.1)
    finally:
        release.set()
        old.join(5)
        current.join(5)
    assert current_done.is_set()
    assert read_snapshot(credential)["blocked_until"] == 9999999999


def test_api_http_auth_tools_continuation_errors_and_cooldown(api_key, monkeypatch):
    seen = []
    fail_status = None
    patch = "*** Begin Patch\n*** End Patch"

    def upstream(request, *, timeout):
        seen.append(request)
        assert request.full_url == API_UPSTREAM + "/responses"
        assert request.get_header("Authorization") == "Bearer api-secret"
        assert not any(name.lower().startswith(("x-grok", "x-xai")) for name in request.headers)
        headers = Message()
        if fail_status is not None:
            headers["Retry-After"] = "120"
            raise urllib.error.HTTPError(
                request.full_url,
                fail_status,
                "error",
                headers,
                BytesIO(b"private detail api-secret"),
            )
        headers["Content-Type"] = "text/event-stream"
        events = [
            {
                "type": "response.output_item.added",
                "item": {
                    "id": "patch-id",
                    "type": "function_call",
                    "call_id": "call-id",
                    "name": "agentroute_apply_patch",
                    "arguments": "",
                },
            },
            {
                "type": "response.function_call_arguments.done",
                "item_id": "patch-id",
                "arguments": json.dumps({"input": patch}),
            },
            {
                "type": "response.completed",
                "response": {"output": [], "usage": {"output_tokens": 9}},
            },
        ]
        response = BytesIO(b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events))
        response.headers = headers
        return response

    monkeypatch.setattr("agentroute.grok_bridge.open_upstream", upstream)
    server = GrokServer(0, GrokApiCredential())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://127.0.0.1:{server.server_port}"
    body = {
        "model": "grok-4.7",
        "tools": [{"type": "custom", "name": "apply_patch"}],
        "input": [
            {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c", "input": patch},
            {"type": "custom_tool_call_output", "call_id": "c", "output": "applied"},
            {"type": "reasoning", "summary": [], "encrypted_content": "reasoning-opaque"},
        ],
    }

    def request(payload=None):
        return urllib.request.Request(
            address + "/v1/responses",
            data=json.dumps(payload or body).encode(),
            headers={"Authorization": "Bearer ar-grok-local-secret"},
        )

    try:
        health = json.load(urllib.request.urlopen(address + "/healthz"))
        assert health["credential_mode"] == "api-key"
        nonce = "ab" * 32
        proof = json.load(urllib.request.urlopen(address + "/healthz?nonce=" + nonce))
        assert proof["credential_proof"] == GrokApiCredential().health_proof(
            nonce, "", server.server_port
        )
        assert proof["credential_proof"] != GrokApiCredential().health_proof(
            nonce, "another-instance", server.server_port
        )
        assert proof["credential_proof"] != GrokApiCredential().health_proof(
            nonce, "", server.server_port + 1
        )
        assert "api-secret" not in json.dumps(proof)
        assert "ar-grok-local-secret" not in json.dumps(proof)
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(address + "/healthz?nonce=invalid")
        assert error.value.code == 400
        models = json.load(urllib.request.urlopen(address + "/v1/models"))
        assert "grok-4.7-build-fast" not in {model["id"] for model in models["data"]}
        for auth in (None, "Bearer api-secret", "Bearer wrong"):
            unauthenticated = urllib.request.Request(
                address + "/v1/responses",
                data=json.dumps(body).encode(),
                headers={"Authorization": auth} if auth else {},
            )
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(unauthenticated)
            assert error.value.code == 401
        assert seen == []
        with urllib.request.urlopen(request()) as response:
            stream = response.read()
        assert b"custom_tool_call" in stream and b"*** Begin Patch" in stream
        events = [json.loads(row[6:]) for row in stream.splitlines() if row.startswith(b"data: ")]
        assert events[-1]["response"]["usage"]["output_tokens"] == 9
        sent = json.loads(seen[-1].data)
        assert sent["tools"][0]["type"] == "function"
        assert sent["input"][1]["type"] == "function_call_output"
        assert sent["input"][2]["encrypted_content"] == "reasoning-opaque"
        assert sent["stream"] is True and sent["store"] is False
        usage = json.load(urllib.request.urlopen(address + "/usage"))
        assert usage == {"credential_mode": "api-key", "billing": "unavailable"}
        assert len(seen) == 1  # No subscription allowance request.
        for fail_status in (401, 429):
            before = len(seen)
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request())
            assert error.value.code == fail_status
            assert b"private" not in error.value.read()
            assert len(seen) == before + 1  # Never replay an upstream request.
        assert read_snapshot(GrokApiCredential())["credential_mode"] == "api-key"
        assert capacity_snapshot(address + "/v1", offline=True)["blocked_until"] > 0
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request({"model": "grok-4.7-build-fast"}))
        assert error.value.code == 400
        assert len(seen) == 3
        upstream_path = Path(os.environ["AGENTROUTE_GROK_API_CREDENTIALS_FILE"])
        upstream_path.write_text("export OTHER_KEY=preserve\n")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request())
        assert error.value.code == 400
        assert len(seen) == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_invalid_api_key_import_preserves_working_key(api_key, monkeypatch):
    path = Path(os.environ['AGENTROUTE_GROK_API_CREDENTIALS_FILE'])
    before = path.read_bytes()
    monkeypatch.setenv('SOURCE_KEY', 'bad-key\n')
    result = CliRunner().invoke(app, ['credential-import', 'SOURCE_KEY'])
    assert result.exit_code != 0
    assert path.read_bytes() == before


def test_status_reports_foreground_api_bridge(monkeypatch, tmp_path):
    from agentroute.bridge_service import BridgeHealth

    monkeypatch.setattr('agentroute.grok_cli.unit_path', lambda: tmp_path / 'absent')
    monkeypatch.setattr('agentroute.grok_cli.wait_for_health', lambda *a, **k: BridgeHealth(
        'healthy', 'api', {'provider': 'grok', 'credential_mode': 'api-key'}
    ))
    result = CliRunner().invoke(app, ['status'])
    assert result.exit_code == 0
    assert json.loads(result.output)['health']['credential_mode'] == 'api-key'
