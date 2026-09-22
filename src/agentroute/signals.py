from __future__ import annotations

import re
from collections.abc import Iterable

from .models import ReasonCode, RouteContext, ScoreContribution

MECHANICAL = re.compile(
    r"\b(rename|typo|format|lint|update (?:the )?import|change (?:the )?(?:label|text)|"
    r"add (?:a )?field|simple crud|bump (?:the )?version)\b",
    re.IGNORECASE,
)
READ_ONLY_RETRIEVAL = re.compile(
    r"(?:\b(?:fetch|get|pull|read|open|show|list|look\s+up|check|inspect)\b"
    r"[^\n]{0,100}\b(?:comment|issue|pr|pull request|status|thread|page|url|link)\b)"
    r"|(?:\b(?:fetch|get|pull|read|open|show|check|inspect)\b[^\n]{0,140}https?://)",
    re.IGNORECASE,
)
SIMPLE_CONTEXT_QUESTION = re.compile(
    r"^\s*(?:did(?:n't| not)?\s+we|did\s+we|have\s+we|had\s+we|"
    r"was(?:n't| not)?\s+(?:that|this)|what\s+did\s+(?:he|she|they|we))\b"
    r"[^\n]{0,180}[?]?\s*$",
    re.IGNORECASE,
)
READ_ONLY_STATUS = re.compile(
    r"(?:\bany\s+outstanding\s+(?:commits?|changes?|issues?|tasks?|prs?|pull requests?)\b)"
    r"|(?:^\s*(?:is|are|was|were|has|have)\b[^\n]{0,120}"
    r"\b(?:fixed|solved|resolved|working|healthy|up|done|merged|deployed|pushed)\b[?]?\s*$)"
    r"|(?:\b(?:status|state)\s+of\b)",
    re.IGNORECASE,
)
BOUNDED_COMMUNICATION = re.compile(
    r"^\s*(?:please\s+)?(?:reply|respond|send|post)\b[^\n]{0,160}"
    r"\b(?:slack|thread|message|comment|email)\b[^\n]{0,80}$",
    re.IGNORECASE,
)
CREDENTIAL_EXPOSURE = re.compile(
    r"(?:\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|"
    r"password|secret)\b\s*(?:is\s+|[:=]\s*)[\"']?"
    r"(?!\[?(?:redacted|hidden|masked)\]?\b)[A-Za-z0-9_./+=-]{12,})"
    r"|(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)",
    re.IGNORECASE,
)
OPERATIONAL_INCIDENT = re.compile(
    r"\b(?:production|prod|outage|incident|critical alert|service unavailable|flapping)\b",
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
    r"^\s*(yes|yep|yeah|ok(?:ay)?(?:,?\s+(?:do it|go ahead|proceed))?|"
    r"do it|go ahead|continue|implement (?:it|that)|proceed|"
    r"sounds good)[.!]?\s*$",
    re.I,
)
CONTEXT_FOLLOWUP = re.compile(
    r"^\s*(?:check|try|run|look|test)\s+(?:it\s+)?again[.!?]?\s*$"
    r"|^\s*so\s+how\s+(?:do\s+we\s+|to\s+)?fix(?:\s+(?:it|that|this))?[?]?\s*$"
    r"|^\s*what\s+about\s+(?:it|that|this)[?]?\s*$"
    r"|^\s*i\s+(?:meant|wanted)\s+(?:you\s+)?to\b",
    re.IGNORECASE,
)
MANUAL = re.compile(r"^\s*@(?P<tier>fast|normal|smart|max|auto)\b[: ]*", re.I)
REASONING_EFFORT = re.compile(
    r"^\s*@(?P<effort>none|minimal|low|medium|high|xhigh|ultra|persistent)\b[: ]*",
    re.I,
)


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
        bool(READ_ONLY_RETRIEVAL.search(prompt)),
        ReasonCode.READ_ONLY_RETRIEVAL,
        -1.5,
        "read-only retrieval wording",
    )
    _add(
        output,
        bool(SIMPLE_CONTEXT_QUESTION.search(prompt)),
        ReasonCode.SIMPLE_CONTEXT_QUESTION,
        -1,
        "simple question about existing context",
    )
    _add(
        output,
        bool(READ_ONLY_STATUS.search(prompt)),
        ReasonCode.READ_ONLY_STATUS,
        -1.5,
        "read-only status question",
    )
    _add(
        output,
        bool(BOUNDED_COMMUNICATION.search(prompt)),
        ReasonCode.BOUNDED_COMMUNICATION,
        -0.25,
        "bounded communication action",
    )
    _add(
        output,
        bool(CREDENTIAL_EXPOSURE.search(prompt)),
        ReasonCode.CREDENTIAL_EXPOSURE,
        4,
        "credential-shaped value in prompt",
    )
    _add(
        output,
        bool(OPERATIONAL_INCIDENT.search(prompt)),
        ReasonCode.OPERATIONAL_INCIDENT,
        3.5,
        "production or operational incident wording",
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


def _backend_manual(backends: Iterable[str] | None) -> re.Pattern[str] | None:
    """Build a prefix matcher from configured backend names."""
    if backends is None:
        names = ("gpt", "azure", "deepseek")
    else:
        names = tuple(str(name) for name in backends)
    if not names:
        return None
    choices = "|".join(sorted((re.escape(name) for name in names), key=len, reverse=True))
    return re.compile(rf"^\s*@(?P<backend>{choices})\b", re.IGNORECASE)


def route_overrides(
    prompt: str, backends: Iterable[str] | None = None
) -> tuple[str | None, str | None, str | None, str]:
    """Parse tier, backend, and reasoning prefixes in any order."""
    tier: str | None = None
    backend: str | None = None
    reasoning_effort: str | None = None
    remaining = prompt
    backend_manual = _backend_manual(backends)
    for _ in range(3):
        tier_match = MANUAL.match(remaining)
        backend_match = backend_manual.match(remaining) if backend_manual else None
        effort_match = REASONING_EFFORT.match(remaining)
        if tier_match and tier is None:
            tier = tier_match.group("tier").lower()
            remaining = remaining[tier_match.end() :].lstrip()
            continue
        if backend_match and backend is None:
            backend = backend_match.group("backend").lower()
            remaining = remaining[backend_match.end() :].lstrip()
            continue
        if effort_match and reasoning_effort is None:
            reasoning_effort = effort_match.group("effort").lower()
            remaining = remaining[effort_match.end() :].lstrip()
            continue
        break
    return tier, backend, reasoning_effort, remaining


def is_confirmation(prompt: str) -> bool:
    return bool(CONFIRMATION.match(prompt))


def contains_credential(text: str | None) -> bool:
    return bool(text and CREDENTIAL_EXPOSURE.search(text))


def continues_previous_task(prompt: str) -> bool:
    """Recognize approvals that also append a follow-up question or constraint."""
    if is_confirmation(prompt) or is_context_followup(prompt):
        return True
    return bool(
        re.match(
            r"^\s*(?:yes|yep|yeah|ok(?:ay)?|do it|go ahead|continue|proceed|sounds good)"
            r"(?:\s+do it)?[.!,:;\-]+\s+\S",
            prompt,
            re.IGNORECASE,
        )
    )


def is_context_followup(prompt: str) -> bool:
    return bool(CONTEXT_FOLLOWUP.match(prompt))


def reason_codes(contributions: Iterable[ScoreContribution]) -> list[ReasonCode]:
    return list(dict.fromkeys(item.code for item in contributions))
