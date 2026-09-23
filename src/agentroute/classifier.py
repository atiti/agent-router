from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import stat
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from .config import ClassifierConfig, JevShadowConfig
from .models import RouteContext, Tier

JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)

CLASSIFIER_SYSTEM_PROMPT = """You route coding-agent turns to the cheapest sufficient model tier
and choose its reasoning effort independently.
Judge the resolved task, not the length of the latest message.
FAST: greetings, acknowledgments, no-op/status checks, exact retrieval, deterministic formatting,
renames, or typo fixes. Do not select FAST for authored communication, explanation, analysis,
implementation, tool orchestration, or judgment even when the latest message is short.
NORMAL: routine communication, explanation, analysis, or implementation with limited uncertainty.
SMART: debugging, production operations, security, architecture, substantial judgment, or
multi-step tool orchestration.
MAX: exceptional cross-cutting reasoning where SMART is materially likely to fail.
Reasoning effort is separate from model tier. Choose LOW for deterministic work, MEDIUM for routine
judgment, HIGH for difficult reasoning, and XHIGH only when unusually deep reasoning is material.
Return JSON only with: tier (FAST|NORMAL|SMART|MAX), reasoning_effort
(LOW|MEDIUM|HIGH|XHIGH), confidence (0..1), task_type (one of greeting, acknowledgment,
no_op, status, retrieval, formatting, communication, explanation, analysis, implementation,
debugging, operations, architecture, security, research, orchestration, other), and reason
(one sentence). Confidence measures certainty that this is the cheapest sufficient tier and effort.
Treat a confirmation as approval of the task described in prior assistant context. Otherwise,
classify the action requested by the latest message; use prior assistant context only as supporting
facts, not as work that must be repeated.
"""


class ClassifierTaskType(str, Enum):
    GREETING = "greeting"
    ACKNOWLEDGMENT = "acknowledgment"
    NO_OP = "no_op"
    STATUS = "status"
    RETRIEVAL = "retrieval"
    FORMATTING = "formatting"
    COMMUNICATION = "communication"
    EXPLANATION = "explanation"
    ANALYSIS = "analysis"
    IMPLEMENTATION = "implementation"
    DEBUGGING = "debugging"
    OPERATIONS = "operations"
    ARCHITECTURE = "architecture"
    SECURITY = "security"
    RESEARCH = "research"
    ORCHESTRATION = "orchestration"
    OTHER = "other"


class ClassifierResult(BaseModel):
    tier: Tier
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] | None = None
    confidence: float = Field(ge=0, le=1)
    task_type: ClassifierTaskType
    reason: str = Field(min_length=1, max_length=500)

    @classmethod
    def from_payload(cls, payload: object) -> ClassifierResult:
        if not isinstance(payload, dict):
            raise ValueError("classifier response must be an object")
        normalized = dict(payload)
        normalized["tier"] = Tier.parse(str(normalized.get("tier", "")))
        if normalized.get("reasoning_effort") is not None:
            normalized["reasoning_effort"] = str(normalized["reasoning_effort"]).lower()
        normalized["task_type"] = normalize_task_type(str(normalized.get("task_type", "")))
        return cls.model_validate(normalized)

    @property
    def reason_hash(self) -> str:
        return hashlib.sha256(self.reason.encode("utf-8")).hexdigest()


class JevShadowResult(BaseModel):
    """A typed, local-only System 1 observation; never a routing authority."""

    tier: Tier
    reasoning_effort: Literal["low", "medium", "high", "xhigh"]
    task_type: ClassifierTaskType
    tier_confidence: float = Field(ge=0, le=1)
    reasoning_effort_confidence: float = Field(ge=0, le=1)
    task_type_confidence: float = Field(ge=0, le=1)
    latency_ms: float = Field(ge=0)
    model: str


def normalize_task_type(value: str) -> ClassifierTaskType:
    """Map classifier wording onto a stable, low-cardinality audit taxonomy."""
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    exact = {item.value: item for item in ClassifierTaskType}
    if normalized in exact:
        return exact[normalized]
    keyword_groups = (
        (ClassifierTaskType.SECURITY, ("security", "auth", "permission", "credential")),
        (ClassifierTaskType.ARCHITECTURE, ("architecture", "design", "migration")),
        (ClassifierTaskType.DEBUGGING, ("debug", "troubleshoot", "root_cause", "fix")),
        (ClassifierTaskType.OPERATIONS, ("production", "deploy", "release", "merge", "operation")),
        (ClassifierTaskType.ORCHESTRATION, ("orchestrat", "subagent", "multi_agent", "workflow")),
        (ClassifierTaskType.IMPLEMENTATION, ("implement", "build", "code", "change")),
        (ClassifierTaskType.RESEARCH, ("research", "investigat", "audit", "review")),
        (ClassifierTaskType.ANALYSIS, ("analy", "evaluate", "compare", "forecast")),
        (ClassifierTaskType.EXPLANATION, ("explain", "question", "clarif")),
        (ClassifierTaskType.COMMUNICATION, ("reply", "message", "email", "slack", "communicat")),
        (ClassifierTaskType.RETRIEVAL, ("retriev", "lookup", "fetch", "file_reference")),
        (ClassifierTaskType.FORMATTING, ("format", "rewrite", "rename", "typo")),
        (ClassifierTaskType.STATUS, ("status", "confirmation", "identifier")),
        (
            ClassifierTaskType.ACKNOWLEDGMENT,
            ("acknowledg", "confirm_proceed", "continue_conversation"),
        ),
        (ClassifierTaskType.GREETING, ("greeting", "hello")),
        (ClassifierTaskType.NO_OP, ("no_op", "empty")),
    )
    for task_type, keywords in keyword_groups:
        if any(keyword in normalized for keyword in keywords):
            return task_type
    return ClassifierTaskType.OTHER


class TierClassifier(Protocol):
    source: str
    last_status: str
    last_error_type: str | None

    def classify(self, context: RouteContext) -> ClassifierResult: ...


def is_loopback_endpoint(endpoint: str) -> bool:
    parsed = urllib.parse.urlparse(endpoint)
    return parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def is_private_endpoint(endpoint: str) -> bool:
    parsed = urllib.parse.urlparse(endpoint)
    if is_loopback_endpoint(endpoint):
        return True
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        return False
    tailscale = ipaddress.ip_network("100.64.0.0/10")
    return address.is_private or address in tailscale


def catalog_endpoint(endpoint: str) -> str:
    parsed = urllib.parse.urlparse(endpoint)
    marker = "/chat/completions"
    path = parsed.path
    if not path.endswith(marker):
        raise ValueError("classifier endpoint must end with /chat/completions")
    return urllib.parse.urlunparse(parsed._replace(path=path[: -len(marker)] + "/models"))


def read_api_key(config: ClassifierConfig) -> str | None:
    value = os.environ.get(config.api_key_env)
    if value:
        return value.strip()
    if not config.api_key_file:
        return None
    path = Path(config.api_key_file).expanduser()
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise RuntimeError(
            f"classifier credential must be a regular file owned by this user: {path}"
        )
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o077:
        raise RuntimeError(f"classifier credential file must be private (chmod 600): {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"classifier credential file is empty: {path}")
    return value


def catalog_age_seconds(config: ClassifierConfig) -> float | None:
    if not config.catalog_checked_at:
        return None
    checked = datetime.fromisoformat(config.catalog_checked_at.replace("Z", "+00:00"))
    return max(0.0, (datetime.now(timezone.utc) - checked).total_seconds())


class OpenAICompatibleClassifier:
    def __init__(self, config: ClassifierConfig) -> None:
        self.config = config
        parsed = urllib.parse.urlparse(config.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("classifier endpoint must be an HTTP(S) URL")
        catalog_endpoint(config.endpoint)
        if parsed.scheme == "http" and not is_loopback_endpoint(config.endpoint):
            if not config.allow_private_http or not is_private_endpoint(config.endpoint):
                raise ValueError(
                    "remote HTTP classifiers must be a private IP and use --allow-private-http"
                )
        self.source = (
            "local_llm"
            if is_loopback_endpoint(config.endpoint)
            else "private_llm"
            if is_private_endpoint(config.endpoint)
            else "cloud_llm"
        )
        self.last_response_metadata: dict[str, object] = {}
        self.last_request_hash: str | None = None
        self.last_latency_ms: float | None = None
        self.last_usage: dict[str, int | float] = {}
        self.last_previous_context_chars = 0
        self.last_status = "idle"
        self.last_error_type: str | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "User-Agent": "agentroute/0.4"}
        api_key = read_api_key(self.config)
        if self.source != "local_llm" and not api_key:
            location = self.config.api_key_file or self.config.api_key_env
            raise RuntimeError(f"classifier credential is missing: {location}")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def verify_catalog(self) -> tuple[list[str], str, str]:
        if self.source != "local_llm" and not self.config.allow_remote:
            raise RuntimeError("remote classifier prompt egress is not enabled")
        request = urllib.request.Request(
            catalog_endpoint(self.config.endpoint), headers=self._headers(), method="GET"
        )
        with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
            payload = json.loads(response.read())
        models = sorted(
            str(item["id"])
            for item in payload.get("data", [])
            if isinstance(item, dict) and item.get("id")
        )
        if self.config.model not in models:
            raise RuntimeError(
                f"classifier model is not visible in provider catalog: {self.config.model}"
            )
        digest = hashlib.sha256(json.dumps(models, separators=(",", ":")).encode()).hexdigest()
        checked_at = datetime.now(timezone.utc).isoformat()
        return models, digest, checked_at

    def classify(self, context: RouteContext) -> ClassifierResult:
        self.last_response_metadata = {}
        self.last_request_hash = None
        self.last_latency_ms = None
        self.last_usage = {}
        self.last_previous_context_chars = 0
        self.last_status = "started"
        self.last_error_type = None
        if self.source != "local_llm" and not self.config.allow_remote:
            raise RuntimeError("remote classifier prompt egress is not enabled")
        catalog_age = catalog_age_seconds(self.config)
        if self.source != "local_llm" and catalog_age is None:
            raise RuntimeError("remote classifier catalog has not been verified")
        if (
            self.source != "local_llm"
            and catalog_age is not None
            and catalog_age > self.config.catalog_ttl_seconds
        ):
            raise RuntimeError("classifier catalog verification is stale")
        if self.config.catalog_models and self.config.model not in self.config.catalog_models:
            raise RuntimeError("configured classifier model is absent from the verified catalog")

        previous = context.task_definition or ""
        if not self.config.include_previous_assistant:
            previous = ""
        previous = previous[-self.config.max_context_chars :]
        classifier_input = {
            "latest_user_message": context.latest_prompt,
            "previous_assistant_context": previous,
            "current_tier": str(context.current_tier),
            "candidate_tiers": ["FAST", "NORMAL", "SMART", "MAX"],
            "candidate_reasoning_efforts": ["LOW", "MEDIUM", "HIGH", "XHIGH"],
        }
        classifier_input_json = json.dumps(
            classifier_input, sort_keys=True, separators=(",", ":")
        )
        self.last_previous_context_chars = len(previous)
        payload: dict[str, object] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": classifier_input_json},
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": self.config.max_completion_tokens,
        }
        if self.config.reasoning_effort:
            payload["reasoning_effort"] = self.config.reasoning_effort
        body = json.dumps(payload).encode("utf-8")
        self.last_request_hash = hashlib.sha256(body).hexdigest()
        request = urllib.request.Request(
            self.config.endpoint,
            data=body,
            headers=self._headers(),
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                response_payload = json.loads(response.read())
        except Exception as error:
            timed_out = isinstance(error, (TimeoutError, socket.timeout)) or "timed out" in str(
                error
            ).lower()
            self.last_status = "timeout" if timed_out else "error"
            self.last_error_type = "timeout" if timed_out else type(error).__name__
            raise
        finally:
            self.last_latency_ms = round((time.perf_counter() - started) * 1_000, 1)
        self.last_response_metadata = {
            key: response_payload[key]
            for key in ("id", "model", "created", "system_fingerprint")
            if response_payload.get(key) is not None
        }
        usage = response_payload.get("usage")
        self.last_usage = (
            {
                str(key): value
                for key, value in usage.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
            if isinstance(usage, dict)
            else {}
        )
        content = response_payload["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("classifier returned no text content")
        match = JSON_FENCE.match(content)
        if match:
            content = match.group(1)
        try:
            result = ClassifierResult.from_payload(json.loads(content))
        except Exception as error:
            self.last_status = "error"
            self.last_error_type = type(error).__name__
            raise
        self.last_status = "succeeded"
        return result


class JevShadowClassifier:
    """Call local-jev's System 1 endpoint with bounded, typed choices.

    This intentionally has no dependency on the production classifier path.  It
    is only used to collect calibration evidence in an audit receipt.
    """

    def __init__(self, config: JevShadowConfig) -> None:
        self.config = config
        parsed = urllib.parse.urlparse(config.endpoint)
        if parsed.scheme != "http" or not is_loopback_endpoint(config.endpoint):
            raise ValueError("Jev shadow endpoint must use loopback HTTP")
        if not parsed.path.rstrip("/").endswith("/v1/systemone"):
            raise ValueError("Jev shadow endpoint must end with /v1/systemone")
        self.last_status = "idle"
        self.last_error_type: str | None = None
        self.source = "local_jev"
        self.last_response_metadata: dict[str, object] = {}
        self.last_request_hash: str | None = None
        self.last_latency_ms: float | None = None
        self.last_usage: dict[str, int | float] = {}
        self.last_previous_context_chars = 0

    def evaluate(self, context: RouteContext) -> JevShadowResult:
        self.last_status = "started"
        self.last_error_type = None
        self.last_response_metadata = {}
        self.last_request_hash = None
        self.last_latency_ms = None
        self.last_usage = {}
        previous = context.task_definition or ""
        if not self.config.include_previous_assistant:
            previous = ""
        previous = previous[-self.config.max_context_chars :]
        self.last_previous_context_chars = len(previous)
        state = context.latest_prompt
        if previous:
            state = f"Latest user request:\n{state}\n\nPrior task context:\n{previous}"
        payload = {
            "model": self.config.model,
            "state": state,
            "questions": {
                "tier": {
                    "type": "choice",
                    "instructions": "Choose the cheapest sufficient coding-agent tier.",
                    "criteria": {
                        "fast": "Exact retrieval, status, no-op, formatting, rename, or typo fix.",
                        "normal": (
                            "Routine communication, explanation, analysis, or implementation."
                        ),
                        "smart": (
                            "Debugging, operations, architecture, security, or multi-step work."
                        ),
                        "max": (
                            "Unusually deep cross-cutting reasoning where smart is materially "
                            "insufficient."
                        ),
                    },
                },
                "reasoning_effort": {
                    "type": "choice",
                    "instructions": "Choose reasoning effort independently from tier.",
                    "criteria": {
                        "low": "Deterministic or exact work.",
                        "medium": "Routine judgment.",
                        "high": "Difficult reasoning or meaningful uncertainty.",
                        "xhigh": "Unusually deep reasoning is materially needed.",
                    },
                },
                "task_type": {
                    "type": "choice",
                    "instructions": "Choose the best matching task type.",
                    "criteria": {
                        item.value: item.value.replace("_", " ")
                        for item in ClassifierTaskType
                    },
                },
            },
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.last_request_hash = hashlib.sha256(body).hexdigest()
        request = urllib.request.Request(
            self.config.endpoint,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "agentroute/0.5"},
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                response_payload = json.loads(response.read())
        except Exception as error:
            timed_out = isinstance(error, (TimeoutError, socket.timeout)) or "timed out" in str(
                error
            ).lower()
            self.last_status = "timeout" if timed_out else "error"
            self.last_error_type = "timeout" if timed_out else type(error).__name__
            raise
        latency_ms = round((time.perf_counter() - started) * 1_000, 1)
        self.last_latency_ms = latency_ms
        try:
            answers = response_payload["answers"]
            tier = Tier.parse(str(answers["tier"]["choice"]))
            reasoning_effort = str(answers["reasoning_effort"]["choice"]).lower()
            task_type = normalize_task_type(str(answers["task_type"]["choice"]))
            result = JevShadowResult(
                tier=tier,
                reasoning_effort=reasoning_effort,
                task_type=task_type,
                tier_confidence=float(answers["tier"]["confidence"]),
                reasoning_effort_confidence=float(answers["reasoning_effort"]["confidence"]),
                task_type_confidence=float(answers["task_type"]["confidence"]),
                latency_ms=latency_ms,
                model=str(response_payload.get("model") or self.config.model),
            )
            self.last_response_metadata = {"model": result.model}
            usage = response_payload.get("usage")
            self.last_usage = (
                {
                    str(key): value
                    for key, value in usage.items()
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                }
                if isinstance(usage, dict)
                else {}
            )
        except Exception as error:
            self.last_status = "error"
            self.last_error_type = type(error).__name__
            raise
        self.last_status = "succeeded"
        return result

    def classify(self, context: RouteContext) -> ClassifierResult:
        """Adapt typed local-jev choices to AgentRoute's live classifier contract."""
        result = self.evaluate(context)
        return ClassifierResult(
            tier=result.tier,
            reasoning_effort=result.reasoning_effort,
            confidence=result.tier_confidence,
            task_type=result.task_type,
            reason="local JEV System 1 typed classification",
        )
