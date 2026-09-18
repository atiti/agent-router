import json
from datetime import datetime, timedelta, timezone

import pytest

from agentroute.classifier import OpenAICompatibleClassifier, read_api_key
from agentroute.config import ClassifierConfig
from agentroute.models import RouteContext, Tier


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


def test_remote_classifier_requires_explicit_prompt_egress():
    classifier = OpenAICompatibleClassifier(
        ClassifierConfig(enabled=True, allow_remote=False)
    )

    with pytest.raises(RuntimeError, match="prompt egress"):
        classifier.classify(RouteContext(session_id="s", latest_prompt="handle this"))


def test_local_classifier_needs_no_api_key_and_parses_json(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse(
            {
                "model": "qwen3:4b",
                "usage": {"prompt_tokens": 42, "completion_tokens": 9, "ignored": "text"},
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "tier": "SMART",
                                    "confidence": 0.87,
                                    "task_type": "debugging",
                                    "reason": "The task requires multi-step diagnosis.",
                                }
                            )
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    classifier = OpenAICompatibleClassifier(
        ClassifierConfig(
            enabled=True,
            endpoint="http://127.0.0.1:11434/v1/chat/completions",
            model="qwen3:4b",
            max_context_chars=8,
        )
    )
    result = classifier.classify(
        RouteContext(
            session_id="s",
            latest_prompt="ok do it",
            task_definition="A long task definition",
            current_tier=Tier.NORMAL,
        )
    )

    assert result.tier is Tier.SMART
    assert result.confidence == 0.87
    assert captured["timeout"] == 5.0
    assert captured["request"].get_header("Authorization") is None
    body = json.loads(captured["request"].data)
    classifier_input = json.loads(body["messages"][1]["content"])
    assert classifier_input["previous_assistant_context"] == "finition"
    assert len(classifier.last_request_hash or "") == 64
    assert classifier.last_latency_ms is not None
    assert classifier.last_usage == {"prompt_tokens": 42, "completion_tokens": 9}
    assert classifier.last_previous_context_chars == 8
    assert classifier.last_status == "succeeded"
    assert classifier.last_error_type is None


def test_classifier_timeout_clears_stale_usage(monkeypatch):
    def timed_out(request, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", timed_out)
    classifier = OpenAICompatibleClassifier(
        ClassifierConfig(
            enabled=True,
            endpoint="http://127.0.0.1:11434/v1/chat/completions",
        )
    )
    classifier.last_usage = {"prompt_tokens": 999}

    with pytest.raises(TimeoutError):
        classifier.classify(RouteContext(session_id="s", latest_prompt="handle this"))

    assert classifier.last_status == "timeout"
    assert classifier.last_error_type == "timeout"
    assert classifier.last_usage == {}


def test_public_remote_http_endpoint_is_rejected():
    with pytest.raises(ValueError, match="private IP"):
        OpenAICompatibleClassifier(
            ClassifierConfig(
                enabled=True,
                endpoint="http://models.example.com/v1/chat/completions",
                allow_remote=True,
            )
        )


def test_tailscale_http_requires_explicit_private_transport_permission():
    with pytest.raises(ValueError, match="allow-private-http"):
        OpenAICompatibleClassifier(
            ClassifierConfig(
                enabled=True,
                endpoint="http://100.64.0.42:4000/v1/chat/completions",
                allow_remote=True,
            )
        )

    classifier = OpenAICompatibleClassifier(
        ClassifierConfig(
            enabled=True,
            endpoint="http://100.64.0.42:4000/v1/chat/completions",
            allow_remote=True,
            allow_private_http=True,
        )
    )
    assert classifier.source == "private_llm"


def test_private_key_file_must_not_be_group_or_world_readable(tmp_path):
    key_file = tmp_path / "classifier.key"
    key_file.write_text("secret")
    key_file.chmod(0o644)
    config = ClassifierConfig(api_key_file=str(key_file))

    with pytest.raises(RuntimeError, match="chmod 600"):
        read_api_key(config)

    key_file.chmod(0o600)
    assert read_api_key(config) == "secret"


def test_catalog_verification_requires_configured_model(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: FakeResponse(
            {"data": [{"id": "classifier-fast"}, {"id": "classifier-smart"}]}
        ),
    )
    classifier = OpenAICompatibleClassifier(
        ClassifierConfig(
            enabled=True,
            endpoint="http://127.0.0.1:11434/v1/chat/completions",
            model="classifier-fast",
        )
    )

    models, digest, checked_at = classifier.verify_catalog()

    assert models == ["classifier-fast", "classifier-smart"]
    assert len(digest) == 64
    assert "+00:00" in checked_at


def test_remote_classification_requires_fresh_verified_catalog(monkeypatch):
    monkeypatch.setenv("AGENTROUTE_CLASSIFIER_API_KEY", "secret")
    classifier = OpenAICompatibleClassifier(
        ClassifierConfig(
            enabled=True,
            endpoint="https://models.example.com/v1/chat/completions",
            model="classifier-model",
            allow_remote=True,
        )
    )
    with pytest.raises(RuntimeError, match="has not been verified"):
        classifier.classify(RouteContext(session_id="s", latest_prompt="handle this"))

    classifier.config.catalog_checked_at = (
        datetime.now(timezone.utc) - timedelta(days=2)
    ).isoformat()
    classifier.config.catalog_models = ["classifier-model"]
    with pytest.raises(RuntimeError, match="stale"):
        classifier.classify(RouteContext(session_id="s", latest_prompt="handle this"))
