"""Tests for the Antigravity (Gemini) Responses bridge."""

from __future__ import annotations

import base64
import json

import pytest

from agentroute import antigravity_bridge as bridge
from agentroute.antigravity_cli import ensure_backend
from agentroute.config import default_config


def keychain_value(payload: dict) -> str:
    return "keyring-marker-1:" + base64.b64encode(json.dumps(payload).encode()).decode()


def test_keychain_payload_decodes_antigravity_token(monkeypatch):
    raw = keychain_value(
        {
            "auth_method": "consumer",
            "id_token_email": "someone@example.com",
            "token": {
                "access_token": "ya29.token",
                "refresh_token": "1//refresh",
                "expiry": "2026-11-26T12:00:00Z",
            },
        }
    )
    decoded = bridge._decode_keychain_payload(raw)
    assert decoded["access_token"] == "ya29.token"
    assert decoded["auth_method"] == "consumer"
    assert decoded["source"] == "keychain"


def test_keychain_payload_rejects_entry_without_token():
    raw = keychain_value({"auth_method": "consumer"})
    with pytest.raises(bridge.CredentialError, match="no access token"):
        bridge._decode_keychain_payload(raw)


def test_file_credential_is_rejected_without_aicode_scope(tmp_path):
    path = tmp_path / "oauth_creds.json"
    path.write_text(
        json.dumps(
            {
                "access_token": "ya29.file",
                "scope": "https://www.googleapis.com/auth/cloud-platform openid",
            }
        )
    )
    with pytest.raises(bridge.CredentialError, match="lacks the auth/aicode scope"):
        bridge.read_file_credential(str(path))


def test_file_credential_is_accepted_with_aicode_scope(tmp_path):
    path = tmp_path / "oauth_creds.json"
    path.write_text(
        json.dumps({"access_token": "ya29.file", "scope": f"openid {bridge.AICODE_SCOPE}"})
    )
    assert bridge.read_file_credential(str(path))["access_token"] == "ya29.file"


def test_credential_prefers_a_keychain_token_that_carries_aicode(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "read_keychain_credential",
        lambda: {"access_token": "keychain-token", "source": "keychain"},
    )
    monkeypatch.setattr(
        bridge,
        "read_file_credential",
        lambda path=None: {"access_token": "file-token", "source": "file"},
    )
    monkeypatch.setattr(
        bridge, "credential_scope", lambda token: f"openid {bridge.AICODE_SCOPE}"
    )
    assert bridge.AntigravityCredential().token() == "keychain-token"


def test_credential_falls_back_when_keychain_token_lacks_scope(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "read_keychain_credential",
        lambda: {"access_token": "keychain-token", "source": "keychain"},
    )
    monkeypatch.setattr(
        bridge,
        "read_file_credential",
        lambda path=None: {"access_token": "file-token", "source": "file"},
    )
    monkeypatch.setattr(
        bridge,
        "credential_scope",
        lambda token: "openid" if token == "keychain-token" else bridge.AICODE_SCOPE,
    )
    assert bridge.AntigravityCredential().token() == "file-token"


def test_prompt_carries_instructions_turns_and_tool_results():
    prompt = bridge.build_prompt(
        {
            "instructions": "You are a reviewer.",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Check the patch."}],
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "tests failed",
                },
            ],
            "tools": [{"type": "function", "name": "shell"}],
        }
    )
    assert "You are a reviewer." in prompt
    assert "Check the patch." in prompt
    assert "tests failed" in prompt
    assert "cannot call the caller's tools" in prompt


def test_prompt_without_content_still_asks_to_continue():
    assert bridge.build_prompt({"input": []}) == "Continue."


def test_usage_is_mapped_and_defaults_to_zero():
    assert bridge.usage_from({"input_tokens": 4, "output_tokens": 2}) == {
        "input_tokens": 4,
        "output_tokens": 2,
        "total_tokens": 6,
    }
    assert bridge.usage_from(None) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }


def test_stream_events_match_the_responses_contract():
    chunks = bridge.response_events("hello", "gemini-3.8-flash-low", None)
    events = [chunk.decode() for chunk in chunks]
    names = [event.split("\n", 1)[0].removeprefix("event: ") for event in events]
    assert names == [
        "response.created",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.output_item.done",
        "response.completed",
    ]
    completed = json.loads(events[-1].split("data: ", 1)[1])
    assert completed["response"]["status"] == "completed"
    text = completed["response"]["output"][0]["content"][0]["text"]
    assert text == "hello"


def test_empty_answer_still_completes_without_a_delta():
    events = [chunk.decode() for chunk in bridge.response_events("", "gemini-3.8-flash-low", None)]
    assert not any("response.output_text.delta" in event for event in events)
    assert "response.completed" in events[-1]


def test_agy_error_status_is_raised_with_its_message(monkeypatch):
    class Completed:
        returncode = 0
        stdout = json.dumps({"status": "ERROR", "response": "", "error": "quota exceeded"})
        stderr = ""

    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **k: Completed())
    with pytest.raises(RuntimeError, match="quota exceeded"):
        bridge.run_agy("prompt", "gemini-3.8-flash-low")


def test_agy_timeout_is_reported(monkeypatch):
    def fake_run(*_args, **_kwargs):
        raise bridge.subprocess.TimeoutExpired(cmd="agy", timeout=1)

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    with pytest.raises(TimeoutError):
        bridge.run_agy("prompt", "gemini-3.8-flash-low", timeout=1)


def test_missing_agy_reports_an_install_hint(monkeypatch):
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    with pytest.raises(bridge.CredentialError, match="not installed"):
        bridge.run_agy("prompt", "gemini-3.8-flash-low")


@pytest.mark.parametrize(
    "message,status",
    [
        ("Antigravity quota is unavailable", 429),
        ("RESOURCE_EXHAUSTED: rate limit", 429),
        ("permission denied", 502),
    ],
)
def test_upstream_failures_map_to_http_status(message, status):
    assert bridge.classify_failure(message) == status


def test_backend_registration_preserves_tier_defaults():
    config = default_config()
    assert ensure_backend(config, 8816) is True
    backend = config.backends["antigravity"]
    assert backend.codex_provider == "agentroute-antigravity"
    assert backend.base_url == "http://127.0.0.1:8816/v1"
    assert backend.tiers["fast"].model == bridge.MODELS["fast"]
    assert backend.tiers["max"].model == bridge.MODELS["max"]
    assert config.routing.backend_by_tier["fast"] == "gpt"


def test_backend_registration_is_idempotent():
    config = default_config()
    ensure_backend(config, 8816)
    ensure_backend(config, 8817)
    assert config.backends["antigravity"].base_url == "http://127.0.0.1:8817/v1"


def test_relative_executable_is_resolved_for_background_services(monkeypatch):
    monkeypatch.setattr(bridge.shutil, "which", lambda name: "/opt/bin/agy")
    monkeypatch.setattr(bridge.os, "access", lambda path, mode: True)
    assert bridge.resolve_agy("agy") == "/opt/bin/agy"


def test_missing_executable_cannot_be_resolved(monkeypatch):
    monkeypatch.setattr(bridge.shutil, "which", lambda name: None)
    with pytest.raises(bridge.CredentialError, match="not installed"):
        bridge.resolve_agy()


def test_absolute_executable_is_used_verbatim(monkeypatch):
    monkeypatch.setattr(bridge.os, "access", lambda path, mode: True)
    assert bridge.resolve_agy("/usr/local/bin/agy") == "/usr/local/bin/agy"


def test_failure_reports_every_credential_source(monkeypatch):
    def keychain_denied():
        raise bridge.CredentialError("no Antigravity credential in Keychain")

    def file_without_scope(path=None):
        raise bridge.CredentialError("the credential file lacks the auth/aicode scope")

    monkeypatch.setattr(bridge, "read_keychain_credential", keychain_denied)
    monkeypatch.setattr(bridge, "read_file_credential", file_without_scope)
    with pytest.raises(bridge.CredentialError) as caught:
        bridge.AntigravityCredential().token()
    message = str(caught.value)
    assert "Keychain" in message
    assert "auth/aicode" in message

