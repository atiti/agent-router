from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from typing import Protocol

from pydantic import BaseModel, Field

from .config import ClassifierConfig
from .models import RouteContext, Tier

JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)

CLASSIFIER_SYSTEM_PROMPT = """You route coding-agent turns to the cheapest sufficient tier.
Judge the resolved task, not the length of the latest message.
FAST: bounded retrieval, status, formatting, renames, or trivial edits.
NORMAL: routine explanation or implementation with limited uncertainty.
SMART: debugging, production operations, security, architecture, substantial judgment, or
multi-step tool orchestration.
MAX: exceptional cross-cutting reasoning where SMART is materially likely to fail.
Return JSON only with: tier (FAST|NORMAL|SMART|MAX), confidence (0..1),
task_type (short snake_case), and reason (one sentence). Confidence measures certainty that this
is the cheapest sufficient tier.
Treat a confirmation as approval of the task described in prior assistant context.
"""


class ClassifierResult(BaseModel):
    tier: Tier
    confidence: float = Field(ge=0, le=1)
    task_type: str = Field(pattern=r"^[a-z0-9_]{1,80}$")
    reason: str = Field(min_length=1, max_length=500)

    @classmethod
    def from_payload(cls, payload: object) -> ClassifierResult:
        if not isinstance(payload, dict):
            raise ValueError("classifier response must be an object")
        normalized = dict(payload)
        normalized["tier"] = Tier.parse(str(normalized.get("tier", "")))
        return cls.model_validate(normalized)

    @property
    def reason_hash(self) -> str:
        return hashlib.sha256(self.reason.encode("utf-8")).hexdigest()


class TierClassifier(Protocol):
    source: str

    def classify(self, context: RouteContext) -> ClassifierResult: ...


def is_loopback_endpoint(endpoint: str) -> bool:
    parsed = urllib.parse.urlparse(endpoint)
    return parsed.hostname in {"127.0.0.1", "localhost", "::1"}


class OpenAICompatibleClassifier:
    def __init__(self, config: ClassifierConfig) -> None:
        self.config = config
        parsed = urllib.parse.urlparse(config.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("classifier endpoint must be an HTTP(S) URL")
        if parsed.scheme == "http" and not is_loopback_endpoint(config.endpoint):
            raise ValueError("remote classifier endpoints must use HTTPS")
        self.source = "local_llm" if is_loopback_endpoint(config.endpoint) else "cloud_llm"

    def classify(self, context: RouteContext) -> ClassifierResult:
        if self.source == "cloud_llm" and not self.config.allow_remote:
            raise RuntimeError("remote classifier prompt egress is not enabled")
        api_key = os.environ.get(self.config.api_key_env)
        if self.source == "cloud_llm" and not api_key:
            raise RuntimeError(f"classifier credential is missing: {self.config.api_key_env}")

        previous = context.task_definition or ""
        if not self.config.include_previous_assistant:
            previous = ""
        previous = previous[-self.config.max_context_chars :]
        classifier_input = {
            "latest_user_message": context.latest_prompt,
            "previous_assistant_context": previous,
            "current_tier": str(context.current_tier),
            "candidate_tiers": ["FAST", "NORMAL", "SMART", "MAX"],
        }
        body = json.dumps(
            {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(classifier_input)},
                ],
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "agentroute/0.2"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            self.config.endpoint,
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
            response_payload = json.loads(response.read())
        content = response_payload["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("classifier returned no text content")
        match = JSON_FENCE.match(content)
        if match:
            content = match.group(1)
        return ClassifierResult.from_payload(json.loads(content))
