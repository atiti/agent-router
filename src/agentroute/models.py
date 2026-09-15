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
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    RISK_FLOOR = "RISK_FLOOR"
    SESSION_AFFINITY = "SESSION_AFFINITY"
    DOWNGRADE_HYSTERESIS = "DOWNGRADE_HYSTERESIS"
    AGENT_ESCALATION = "AGENT_ESCALATION"
    QUOTA_LIMIT = "QUOTA_LIMIT"


class ScoreContribution(BaseModel):
    code: ReasonCode
    weight: float
    detail: str


class RouteContext(BaseModel):
    session_id: str
    provider: str = "codex"
    latest_prompt: str
    current_model: str = ""
    current_tier: Tier = Tier.NORMAL
    previous_task_tier: Tier | None = None
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
    session_id: str
    prompt_hash: str
    manual_override: bool = False
    inherited: bool = False
    switched: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)
