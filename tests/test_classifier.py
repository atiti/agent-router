import json
from datetime import datetime, timedelta, timezone

import pytest

from agentroute.classifier import JevShadowClassifier, OpenAICompatibleClassifier, read_api_key
from agentroute.config import ClassifierConfig, JevShadowConfig, default_config
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


def test_jev_shadow_is_loopback_only_and_parses_typed_answers(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse(
            {
                "model": "nli-deberta-large",
                "answers": {
                    "tier": {"noul": 0.08},
                    "requires_smart": {"noul": 0.82},
                    "requires_max": {"noul": 0.05},
                    "reasoning_effort": {"choice": "high", "confidence": 0.30},
                    "task_type": {"choice": "debugging", "confidence": 0.52},
                },
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    classifier = JevShadowClassifier(JevShadowConfig(timeout_seconds=0.75))
    result = classifier.evaluate(
        RouteContext(session_id="s", latest_prompt="diagnose the production outage")
    )

    assert result.tier is Tier.SMART
    assert result.reasoning_effort == "high"
    assert result.task_type.value == "debugging"
    assert result.tier_confidence == 0.82
    assert result.tier_signals == {
        "clearly_fast": 0.08,
        "requires_smart": 0.82,
        "requires_max": 0.05,
    }
    assert captured["timeout"] == 0.75
    body = json.loads(captured["request"].data)
    assert body["model"] == "nli-deberta-large"
    assert set(body["questions"]) == {
        "tier",
        "requires_smart",
        "requires_max",
        "reasoning_effort",
        "task_type",
    }

    with pytest.raises(ValueError, match="loopback"):
        JevShadowClassifier(JevShadowConfig(endpoint="http://example.com/v1/systemone"))


def test_remote_classifier_requires_explicit_prompt_egress():
    classifier = OpenAICompatibleClassifier(
        ClassifierConfig(enabled=True, allow_remote=False)
    )

    with pytest.raises(RuntimeError, match="prompt egress"):
        classifier.classify(RouteContext(session_id="s", latest_prompt="handle this"))


def test_classifier_reasoning_effort_remains_low_by_default():
    assert default_config().routing.classifier.reasoning_effort == "low"


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
                                    "reasoning_effort": "HIGH",
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
    assert result.reasoning_effort == "high"
    assert result.confidence == 0.87
    assert captured["timeout"] == 5.0
    assert captured["request"].get_header("Authorization") is None
    body = json.loads(captured["request"].data)
    assert body["reasoning_effort"] == "low"
    classifier_input = json.loads(body["messages"][1]["content"])
    assert classifier_input["previous_assistant_context"] == "finition"
    assert classifier_input["candidate_reasoning_efforts"] == [
        "LOW", "MEDIUM", "HIGH", "XHIGH"
    ]
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


@pytest.mark.parametrize("allow_remote,credential", [(False, "test-key"), (True, None)])
def test_hosted_jev_requires_egress_permission_and_credential(
    monkeypatch, allow_remote, credential,
):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    if credential:
        monkeypatch.setenv("TYPESAFE_API_KEY", credential)
    classifier = JevShadowClassifier(JevShadowConfig(
        endpoint="https://api.typesafe.ai/v1/systemone", model="jev-latest",
        allow_remote=allow_remote,
    ))
    with pytest.raises(RuntimeError), pytest.MonkeyPatch.context() as context:
        def never_send(*args, **kwargs):
            pytest.fail("blocked hosted request must not access network")
        context.setattr("urllib.request.urlopen", never_send)
        classifier.evaluate(RouteContext(session_id="s", latest_prompt="draft an email"))


def test_hosted_jev_uses_separate_private_key_and_parses_official_api(tmp_path, monkeypatch):
    key = tmp_path / "typesafe-key"
    key.write_text("hosted-test-key")
    key.chmod(0o600)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    captured = []
    def fake_urlopen(request, timeout):
        captured.append(request)
        assert request.get_header("Authorization") == "Bearer hosted-test-key"
        if request.get_method() == "GET":
            return FakeResponse({"models": [{"name": "jev-latest"}, {"name": "jev-preview"}]})
        return FakeResponse({
            "model": "jev-1.13.0",
            "usage": {"input_tokens": 736, "output_tokens": 254},
            "answers": {
                "tier": {"noul": 0.05}, "requires_smart": {"noul": 0.91},
                "requires_max": {"noul": 0.34},
                "reasoning_effort": {"choice": "high", "confidence": 0.81},
                "task_type": {"choice": "debugging", "confidence": 0.99},
            },
        })
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    classifier = JevShadowClassifier(JevShadowConfig(
        endpoint="https://api.typesafe.ai/v1/systemone", model="jev-latest",
        allow_remote=True, api_key_file=str(key),
    ))
    models, digest, checked = classifier.verify_catalog()
    result = classifier.classify(RouteContext(session_id="s", latest_prompt="diagnose an outage"))
    assert models == ["jev-latest", "jev-preview"]
    assert digest and checked
    assert captured[0].full_url == "https://api.typesafe.ai/v1/models"
    assert captured[1].full_url == "https://api.typesafe.ai/v1/systemone"
    assert classifier.source == "cloud_jev"
    assert result.tier == Tier.SMART
    assert classifier.last_response_metadata == {"model": "jev-1.13.0"}
    assert classifier.last_usage == {"input_tokens": 736, "output_tokens": 254}


@pytest.mark.parametrize("endpoint", [
    "http://api.typesafe.ai/v1/systemone", "ftp://api.typesafe.ai/v1/systemone",
    "https://user:pass@api.typesafe.ai/v1/systemone",
])
def test_hosted_jev_rejects_insecure_or_embedded_credential_urls(endpoint):
    with pytest.raises(ValueError):
        JevShadowClassifier(JevShadowConfig(endpoint=endpoint, allow_remote=True))
