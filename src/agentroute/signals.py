from __future__ import annotations

import re
from collections.abc import Iterable

from .models import ReasonCode, RouteContext, ScoreContribution

MECHANICAL = re.compile(
    r"\b(rename|typo|format|lint|update (?:the )?import|change (?:the )?(?:label|text)|"
    r"add (?:a )?field|simple crud|bump (?:the )?version)\b",
    re.IGNORECASE,
)
DEBUGGING = re.compile(r"\b(debug|root cause|why (?:does|is|did)|still (?:fails|broken))\b", re.I)
ARCHITECTURE = re.compile(
    r"\b(architecture|redesign|rearchitect|system design|cross[- ]cutting)\b", re.I
)
SECURITY = re.compile(
    r"\b(security|vulnerab|oauth|authentication|authorization|permission)\b", re.I
)
CONCURRENCY = re.compile(
    r"\b(race condition|deadlock|concurren|distributed lock|cross[- ]process)\b", re.I
)
MIGRATION = re.compile(r"\b(migration|zero[- ]downtime|schema change|backfill)\b", re.I)
PERFORMANCE = re.compile(r"\b(performance|latency|throughput|memory leak|profil(?:e|ing))\b", re.I)
CONFIRMATION = re.compile(
    r"^\s*(yes|yep|yeah|do it|go ahead|continue|implement (?:it|that)|proceed|"
    r"sounds good)[.!]?\s*$",
    re.I,
)
MANUAL = re.compile(r"^\s*@(?P<tier>fast|normal|smart|max|auto)\b[: ]*", re.I)


def _add(
    output: list[ScoreContribution],
    condition: bool,
    code: ReasonCode,
    weight: float,
    detail: str,
) -> None:
    if condition:
        output.append(ScoreContribution(code=code, weight=weight, detail=detail))


def extract_signals(context: RouteContext) -> list[ScoreContribution]:
    prompt = context.latest_prompt.strip()
    output: list[ScoreContribution] = []
    _add(
        output,
        bool(MECHANICAL.search(prompt)),
        ReasonCode.MECHANICAL_TASK,
        -2,
        "mechanical wording",
    )
    _add(
        output,
        len(prompt) < 120 and prompt.count("\n") < 3,
        ReasonCode.SMALL_SCOPE,
        -0.5,
        "short prompt",
    )
    _add(
        output,
        bool(DEBUGGING.search(prompt)),
        ReasonCode.DEBUGGING,
        2,
        "debugging/root-cause wording",
    )
    _add(
        output,
        bool(ARCHITECTURE.search(prompt)),
        ReasonCode.ARCHITECTURE,
        3,
        "architecture wording",
    )
    _add(output, bool(SECURITY.search(prompt)), ReasonCode.SECURITY, 3, "security/auth wording")
    _add(output, bool(CONCURRENCY.search(prompt)), ReasonCode.CONCURRENCY, 3, "concurrency wording")
    _add(output, bool(MIGRATION.search(prompt)), ReasonCode.MIGRATION, 3, "migration wording")
    _add(output, bool(PERFORMANCE.search(prompt)), ReasonCode.PERFORMANCE, 2, "performance wording")
    _add(
        output,
        len(prompt) > 800 or context.touched_files >= 8 or context.changed_lines >= 500,
        ReasonCode.HIGH_SCOPE,
        1.5,
        "large prompt or repository scope",
    )
    failures = context.repeated_errors + context.failed_tests
    _add(
        output,
        failures >= 2 or context.tool_errors >= 3,
        ReasonCode.REPEATED_FAILURE,
        2.5,
        "repeated tool/test failures",
    )
    return output


def prompt_override(prompt: str) -> tuple[str | None, str]:
    match = MANUAL.match(prompt)
    if not match:
        return None, prompt
    return match.group("tier").lower(), prompt[match.end() :].lstrip()


def is_confirmation(prompt: str) -> bool:
    return bool(CONFIRMATION.match(prompt))


def reason_codes(contributions: Iterable[ScoreContribution]) -> list[ReasonCode]:
    return list(dict.fromkeys(item.code for item in contributions))
