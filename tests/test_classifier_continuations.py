import json

import pytest

from agentroute.classifier import JevShadowClassifier, OpenAICompatibleClassifier
from agentroute.config import ClassifierConfig, JevShadowConfig, default_config
from agentroute.models import RouteContext, Tier
from agentroute.router import Router
from agentroute.signals import continues_previous_task, is_confirmation


@pytest.mark.parametrize("prompt", [
    "ok continue", "OK, continue!", "okay, please continue", "yes, go ahead",
    "please proceed", "continue, please.", "ok keep going", "resume", "finish it",
])
def test_approval_variants_continue_the_existing_task(prompt):
    assert is_confirmation(prompt)
    assert continues_previous_task(prompt)
    decision = Router(default_config()).route(RouteContext(
        session_id="continuation-regression", latest_prompt=prompt,
        current_tier=Tier.SMART, previous_task_tier=Tier.SMART,
        previous_reasoning_effort="high",
    ))
    assert decision.inherited
    assert decision.tier is Tier.SMART
    assert decision.reasoning_effort == "high"


@pytest.mark.parametrize("prompt", [
    "ok, now translate this sentence", "yes. New task: rename this label",
    "okay, instead explain the budget", "continue explaining how DNS works",
    "please proceed with a different project", "go ahead and translate this paragraph",
])
def test_new_substantive_requests_do_not_inherit_by_approval_prefix(prompt):
    assert not is_confirmation(prompt)
    assert not continues_previous_task(prompt)


@pytest.mark.parametrize("prompt", [
    "ok continue. Keep the current API.", "please go ahead. Should it run locally?",
    "yes, but keep the same deployment window", "okay. Can it use the existing config?",
])
def test_approval_can_append_a_related_constraint(prompt):
    assert continues_previous_task(prompt)


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps({
            "choices": [{"message": {"content": json.dumps({
                "tier": "SMART", "reasoning_effort": "HIGH", "confidence": 0.9,
                "task_type": "orchestration", "reason": "Continue the deployment work.",
            })}}],
            "answers": {
                "tier": {"noul": 0.01}, "requires_smart": {"noul": 0.95},
                "requires_max": {"noul": 0.02},
                "reasoning_effort": {"choice": "high", "confidence": 0.9},
                "task_type": {"choice": "orchestration", "confidence": 0.9},
            },
        }).encode()


@pytest.mark.parametrize("engine", ["llm", "jev"])
@pytest.mark.parametrize("include_previous,budget", [(True, 0), (False, 20), (True, 20)])
def test_classifier_context_respects_privacy_and_shared_text_budget(
    monkeypatch, engine, include_previous, budget
):
    captured = {}

    def fake_urlopen(request, timeout):
        captured.update(json.loads(request.data))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    options = dict(include_previous_assistant=include_previous, max_context_chars=budget)
    classifier = (
        OpenAICompatibleClassifier(ClassifierConfig(
            endpoint="http://127.0.0.1:8091/v1/chat/completions", **options,
        )) if engine == "llm" else JevShadowClassifier(JevShadowConfig(**options))
    )
    result = classifier.classify(RouteContext(
        session_id="bounded-context", latest_prompt="ok continue",
        task_definition="PRIVATE_PRIOR_TASK_" * 10,
        ongoing_objective="PRIVATE_OBJECTIVE_" * 10,
        coordination_role="supervisor", previous_task_tier=Tier.SMART,
        failed_tests=3, repeated_errors=2, touched_files=12, changed_lines=600,
    ))
    assert result.task_type.value == "orchestration"
    if engine == "llm":
        body = json.loads(captured["messages"][1]["content"])
        previous = body["previous_assistant_context"]
        objective = body["ongoing_objective"]
        state = body["task_state"]
        assert len(previous) + len(objective) <= (budget if include_previous else 0)
        assert state["coordination_role"] == "supervisor"
        assert state["failed_tests"] == 3
        assert state["previous_task_tier"] == "smart"
    else:
        state = captured["state"]
        assert '"failed_tests":3' in state
        assert '"coordination_role":"supervisor"' in state
        if not include_previous or not budget:
            assert "PRIVATE_" not in state
            assert "Prior task context" not in state
            assert "Ongoing objective" not in state
    if not include_previous or not budget:
        assert classifier.last_previous_context_chars == 0


def test_same_objective_is_not_repeated_in_classifier_context(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured.update(json.loads(request.data))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    classifier = OpenAICompatibleClassifier(ClassifierConfig(
        endpoint="http://127.0.0.1:8091/v1/chat/completions", max_context_chars=20,
    ))
    classifier.classify(RouteContext(
        session_id="dedupe-context", latest_prompt="continue",
        task_definition="Deploy and verify.", ongoing_objective="Deploy and verify.",
    ))
    body = json.loads(captured["messages"][1]["content"])
    assert body["previous_assistant_context"] == ""
    assert body["ongoing_objective"] == "Deploy and verify."
