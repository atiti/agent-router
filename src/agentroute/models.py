from __future__ import annotations

from enum import Enum, IntEnum

from pydantic import BaseModel, Field


class Tier(IntEnum):
    FAST = 0
    NORMAL = 1
    SMART = 2
    MAX = 3

    @classmethod
    def parse(cls, value: str) -> Tier:
        try:
            return cls[value.strip().upper()]
        except KeyError as error:
            raise ValueError(f"unknown tier: {value}") from error

    def __str__(self) -> str:
        return self.name.lower()


class ReasonCode(str, Enum):
    MECHANICAL_TASK = "MECHANICAL_TASK"
    READ_ONLY_RETRIEVAL = "READ_ONLY_RETRIEVAL"
    SIMPLE_CONTEXT_QUESTION = "SIMPLE_CONTEXT_QUESTION"
    READ_ONLY_STATUS = "READ_ONLY_STATUS"
    BOUNDED_COMMUNICATION = "BOUNDED_COMMUNICATION"
    CREDENTIAL_EXPOSURE = "CREDENTIAL_EXPOSURE"
    OPERATIONAL_INCIDENT = "OPERATIONAL_INCIDENT"
    SMALL_SCOPE = "SMALL_SCOPE"
    DEBUGGING = "DEBUGGING"
    ARCHITECTURE = "ARCHITECTURE"
    SECURITY = "SECURITY"
    CONCURRENCY = "CONCURRENCY"
    MIGRATION = "MIGRATION"
    PERFORMANCE = "PERFORMANCE"
    HIGH_SCOPE = "HIGH_SCOPE"
    REPEATED_FAILURE = "REPEATED_FAILURE"
    PREVIOUS_TASK_INHERITANCE = "PREVIOUS_TASK_INHERITANCE"
    TASK_DEFINITION_INHERITANCE = "TASK_DEFINITION_INHERITANCE"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    RISK_FLOOR = "RISK_FLOOR"
    SESSION_AFFINITY = "SESSION_AFFINITY"
    DOWNGRADE_HYSTERESIS = "DOWNGRADE_HYSTERESIS"
    AGENT_ESCALATION = "AGENT_ESCALATION"
    MODEL_COMPATIBILITY_FALLBACK = "MODEL_COMPATIBILITY_FALLBACK"
    LLM_CLASSIFIER = "LLM_CLASSIFIER"
    CLASSIFIER_FALLBACK = "CLASSIFIER_FALLBACK"
    QUOTA_LIMIT = "QUOTA_LIMIT"
    BACKEND_OVERRIDE = "BACKEND_OVERRIDE"
    BACKEND_FALLBACK = "BACKEND_FALLBACK"


class ScoreContribution(BaseModel):
    code: ReasonCode
    weight: float
    detail: str


class RouteContext(BaseModel):
    session_id: str
    turn_id: str | None = None
    provider: str = "codex"
    current_model_provider: str = "openai"
    sticky_backend: str | None = None
    route_scope: str = "root"
    agent_id: str | None = None
    latest_prompt: str
    current_model: str = ""
    current_tier: Tier = Tier.NORMAL
    previous_task_tier: Tier | None = None
    task_definition: str | None = None
    agent_requested_tier: Tier | None = None
    agent_request_reason_hash: str | None = None
    touched_files: int = 0
    changed_lines: int = 0
    tool_errors: int = 0
    repeated_errors: int = 0
    failed_tests: int = 0
    task_risk_flags: set[str] = Field(default_factory=set)


class RouteDecision(BaseModel):
    tier: Tier
    model: str
    reasoning_effort: str | None = None
    confidence: float = Field(ge=0, le=1)
    raw_score: float
    reason_codes: list[ReasonCode]
    contributions: list[ScoreContribution]
    provider: str
    backend: str = "gpt"
    model_provider: str = "openai"
    sticky_backend: str | None = None
    strip_provider_state: bool = False
    route_scope: str = "root"
    agent_id: str | None = None
    session_id: str
    turn_id: str | None = None
    prompt_hash: str
    manual_override: bool = False
    inherited: bool = False
    switched: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)
    proposed_tier: Tier | None = None
    comparison_tier: Tier | None = None
    classifier_version: str = "hybrid-v7"
    classification_source: str = "heuristic"
    classifier_confidence: float | None = Field(default=None, ge=0, le=1)
    classifier_task_type: str | None = None
    classifier_reason_hash: str | None = None
    task_context_used: bool = False
    previous_context_sent: bool = False
    resolved_task_inherited: bool = False
    classifier_latency_ms: float | None = Field(default=None, ge=0)
    classifier_request_hash: str | None = None
    classifier_usage: dict[str, int | float] = Field(default_factory=dict)
    risk_floor_applied: bool = False
    agent_requested_tier: Tier | None = None
    agent_request_reason_hash: str | None = None
    selection_receipt: dict[str, object] = Field(default_factory=dict)
    selection_receipt_hash: str | None = None
