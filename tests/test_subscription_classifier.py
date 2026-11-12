import json
import subprocess

import pytest

from agentroute.config import default_config
from agentroute.models import RouteContext, Tier
from agentroute.router import Router
from agentroute.subscription_classifier import SubscriptionClassifier


def setup_classifier(tmp_path, monkeypatch):
    config = default_config()
    config.routing.classifier.transport = "codex_subscription"
    config.routing.classifier.enabled = True
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"auth_mode": "chatgpt"}))
    monkeypatch.setattr("agentroute.subscription_classifier.codex_home", lambda: tmp_path)
    config.capacity.profiles = {}
    return config, SubscriptionClassifier(config)


def test_dispatch_isolated_authenticated_and_audited(tmp_path, monkeypatch):
    config, classifier = setup_classifier(tmp_path, monkeypatch)
    config.routing.classifier.model = "gpt-6-luna"
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("HC_TERMINAL_ID", "must-not-inherit")

    class Process:
        returncode = 0

        def __init__(self, command, **kwargs):
            assert command[0].endswith("codex-bin")
            assert "--ephemeral" in command and "--ignore-rules" in command
            assert command[command.index("-m") + 1] == "gpt-6-luna"
            assert "hooks" in command
            assert "OPENAI_API_KEY" not in kwargs["env"]
            assert "HC_TERMINAL_ID" not in kwargs["env"]
            assert kwargs["env"]["CODEX_HOME"] != str(tmp_path)
            self.output = command[command.index("-o") + 1]

        def communicate(self, prompt, timeout):
            assert "Diagnose" in prompt
            from pathlib import Path

            Path(self.output).write_text(
                json.dumps(
                    dict(
                        tier="SMART",
                        reasoning_effort="high",
                        confidence=0.91,
                        task_type="debugging",
                        reason="Investigation required",
                    )
                )
            )
            return json.dumps({"type": "turn.completed", "usage": {"input_tokens": 123}}), ""

    monkeypatch.setattr(subprocess, "Popen", Process)
    result = classifier.classify(RouteContext(session_id="test", latest_prompt="Diagnose timeout"))
    assert result.tier == Tier.SMART
    assert classifier.last_status == "succeeded"
    assert classifier.last_usage["input_tokens"] == 123
    assert Router(config).classifier.source == "subscription_llm"


def test_reject_api_key_auth_without_launching(tmp_path, monkeypatch):
    _, classifier = setup_classifier(tmp_path, monkeypatch)
    (tmp_path / "auth.json").write_text('{"auth_mode":"apikey"}')
    with pytest.raises(ValueError, match="ChatGPT subscription"):
        classifier.classify(RouteContext(session_id="test", latest_prompt="hello"))
    assert classifier.last_status == "error"


def test_timeout_kills_only_isolated_process_group(tmp_path, monkeypatch):
    _, classifier = setup_classifier(tmp_path, monkeypatch)
    killed = []

    class Process:
        pid = 12345

        def __init__(self, *args, **kwargs):
            assert kwargs["start_new_session"]
            self.calls = 0

        def communicate(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("codex", 1)
            return "", ""

    monkeypatch.setattr(subprocess, "Popen", Process)
    monkeypatch.setattr("os.killpg", lambda *args: killed.append(args))
    with pytest.raises(subprocess.TimeoutExpired):
        classifier.classify(RouteContext(session_id="test", latest_prompt="hello"))
    assert killed[0][0] == 12345
    assert classifier.last_status == classifier.last_error_type == "timeout"


@pytest.mark.parametrize("failure", ["tool", "malformed", "exit"])
def test_invalid_subprocess_results_are_rejected(tmp_path, monkeypatch, failure):
    _, classifier = setup_classifier(tmp_path, monkeypatch)

    class Process:
        returncode = 1 if failure == "exit" else 0

        def __init__(self, command, **kwargs):
            from pathlib import Path

            self.output = Path(command[command.index("-o") + 1])

        def communicate(self, *args, **kwargs):
            self.output.write_text(
                "{}"
                if failure == "malformed"
                else json.dumps(
                    dict(
                        tier="NORMAL",
                        confidence=0.9,
                        task_type="analysis",
                        reason="Routine analysis",
                    )
                )
            )
            return (
                json.dumps({"type": "item.completed", "item": {"type": "mcp_tool_call"}})
                if failure == "tool"
                else ""
            ), ""

    monkeypatch.setattr(subprocess, "Popen", Process)
    with pytest.raises((RuntimeError, ValueError)):
        classifier.classify(RouteContext(session_id="test", latest_prompt="Explain the result"))
    assert classifier.last_status == "error"


def test_subscription_fallback_timeout_keeps_router_available(tmp_path, monkeypatch):
    config, classifier = setup_classifier(tmp_path, monkeypatch)
    config.routing.mode = "llm"
    config.routing.classifier.engine = "jev"
    from agentroute.classifier import ClassifierResult

    class LowConfidenceJev:
        source = "cloud_jev"

        def classify(self, context):
            return ClassifierResult.from_payload(
                dict(tier="NORMAL", confidence=0.4, task_type="analysis", reason="Ambiguous")
            )

    def timeout(prompt):
        raise subprocess.TimeoutExpired("isolated-codex", 20)

    monkeypatch.setattr(classifier, "_classify", timeout)
    decision = Router(config, classifier=LowConfidenceJev(), fallback_classifier=classifier).route(
        RouteContext(session_id="test", latest_prompt="Explain why this might be failing")
    )
    assert decision.classification_source == "heuristic_fallback"
    assert decision.classifier_status == "timeout"
