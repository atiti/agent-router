import json

import pytest

from agentroute.classifier import OpenAICompatibleClassifier
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
    assert captured["timeout"] == 2.0
    assert captured["request"].get_header("Authorization") is None
    body = json.loads(captured["request"].data)
    classifier_input = json.loads(body["messages"][1]["content"])
    assert classifier_input["previous_assistant_context"] == "finition"


def test_remote_http_endpoint_is_rejected():
    with pytest.raises(ValueError, match="HTTPS"):
        OpenAICompatibleClassifier(
            ClassifierConfig(
                enabled=True,
                endpoint="http://models.example.com/v1/chat/completions",
                allow_remote=True,
            )
        )
