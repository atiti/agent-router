"""Privacy-safe local aggregation of AgentRoute audit usage."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Any

from .classifier import normalize_task_type
from .config import PricingConfig
from .pricing import CostReport, cost_report, token_cost


def _period(created_at: str, bucket: str) -> str:
    value = datetime.fromisoformat(created_at)
    if bucket == "day":
        return value.strftime("%Y-%m-%d")
    if bucket == "week":
        iso = value.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if bucket == "month":
        return value.strftime("%Y-%m")
    raise ValueError(f"unsupported bucket: {bucket}")


def _classifier_model(row: Any) -> str:
    try:
        receipt = json.loads(row["selection_receipt"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return "unknown"
    classifier = receipt.get("classifier", {}) if isinstance(receipt, dict) else {}
    model = classifier.get("model") if isinstance(classifier, dict) else None
    return str(model) if model else "unknown"


def _classifier_usage(row: Any) -> dict[str, int | float]:
    try:
        usage = json.loads(row["classifier_usage"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return usage if isinstance(usage, dict) else {}


@dataclass(frozen=True)
class ModelUsage:
    backend: str
    model: str
    turns: int
    completed_turns: int
    measured_turns: int
    average_duration_ms: float | None
    p50_duration_ms: float | None
    p95_duration_ms: float | None
    maximum_duration_ms: float | None
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    answer_cost: float


@dataclass(frozen=True)
class PeriodUsage(ModelUsage):
    period: str


@dataclass(frozen=True)
class ClassifierUsage:
    model: str
    calls: int
    successful_calls: int
    fallback_calls: int
    timeout_calls: int
    error_calls: int
    success_rate: float
    fallback_rate: float
    timeout_rate: float
    failure_reasons: dict[str, int]
    prompt_tokens: int
    cached_input_tokens: int
    completion_tokens: int
    total_tokens: int
    average_latency_ms: float | None
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    estimated_cost: float


@dataclass(frozen=True)
class DurationSummary:
    completed_turns: int
    average_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    maximum_ms: float | None


@dataclass(frozen=True)
class ReconciliationSummary:
    completed_metered: int
    completed_unmetered: int
    pending: int
    stale_unreconciled: int
    failed: int
    interrupted: int
    superseded: int


@dataclass(frozen=True)
class LongestTurn:
    decision_id: int
    created_at: str
    backend: str
    model: str
    tier: str
    duration_ms: float
    total_tokens: int


@dataclass(frozen=True)
class CapacityUsage:
    warnings: int
    fallbacks: int
    blocked: int
    fallback_completed_turns: int
    fallback_duration_ms: float
    by_route: dict[str, int]
    by_trigger: dict[str, int]


@dataclass(frozen=True)
class CalibrationSummary:
    automatic_turns: int
    labeled_turns: int
    explicit_correct: int
    too_low: int
    too_high: int
    execution_failed: int
    next_manual_override_signals: int
    stronger_next_override_signals: int
    reasoning_effort_sources: dict[str, int]
    reasoning_efforts: dict[str, int]
    task_types: dict[str, int]
    confidence_bands: dict[str, int]


@dataclass(frozen=True)
class AnalyticsReport:
    rows: int
    model_mismatch_turns: int
    route_application: dict[str, int]
    overall: CostReport
    by_model: tuple[ModelUsage, ...]
    over_time: tuple[PeriodUsage, ...]
    classifiers: tuple[ClassifierUsage, ...]
    duration: DurationSummary
    reconciliation: ReconciliationSummary
    longest_turns: tuple[LongestTurn, ...]
    capacity: CapacityUsage
    calibration: CalibrationSummary


def _duration_ms(row: Any) -> float | None:
    if row["turn_duration_ms"] is not None:
        return max(0.0, float(row["turn_duration_ms"]))
    completed_at = row["turn_completed_at"] or row["usage_recorded_at"]
    if not completed_at:
        return None
    try:
        started = datetime.fromisoformat(str(row["created_at"]))
        completed = datetime.fromisoformat(str(completed_at))
    except ValueError:
        return None
    return max(0.0, (completed - started).total_seconds() * 1000)


def _duration_summary(rows: list[Any]) -> DurationSummary:
    values = sorted(value for row in rows if (value := _duration_ms(row)) is not None)
    if not values:
        return DurationSummary(0, None, None, None, None)
    p95_index = max(0, math.ceil(len(values) * 0.95) - 1)
    return DurationSummary(
        completed_turns=len(values),
        average_ms=sum(values) / len(values),
        p50_ms=float(median(values)),
        p95_ms=values[p95_index],
        maximum_ms=values[-1],
    )


def _reconciliation_summary(rows: list[Any], stale_after_hours: int = 24) -> ReconciliationSummary:
    counts = {
        "completed_metered": 0,
        "completed_unmetered": 0,
        "pending": 0,
        "stale_unreconciled": 0,
        "failed": 0,
        "interrupted": 0,
        "superseded": 0,
    }
    now = datetime.now(timezone.utc)
    for row in rows:
        outcome = str(row["turn_outcome"] or "pending")
        if outcome in {"failed", "interrupted", "superseded"}:
            counts[outcome] += 1
        elif row["turn_completed_at"] is not None:
            key = (
                "completed_metered"
                if row["usage_recorded_at"] is not None
                else "completed_unmetered"
            )
            counts[key] += 1
        else:
            created = datetime.fromisoformat(str(row["created_at"])).astimezone(timezone.utc)
            key = (
                "stale_unreconciled"
                if (now - created).total_seconds() >= stale_after_hours * 3600
                else "pending"
            )
            counts[key] += 1
    return ReconciliationSummary(**counts)


def _capacity_summary(rows: list[Any]) -> CapacityUsage:
    warnings = fallbacks = blocked = fallback_completed = 0
    fallback_duration_ms = 0.0
    by_route: dict[str, int] = defaultdict(int)
    by_trigger: dict[str, int] = defaultdict(int)
    for row in rows:
        status = str(row["capacity_status"] or "disabled")
        if status == "warning":
            warnings += 1
        elif status == "fallback":
            fallbacks += 1
            if (duration := _duration_ms(row)) is not None:
                fallback_completed += 1
                fallback_duration_ms += duration
            requested = str(row["capacity_requested_backend"] or "unknown")
            selected = str(row["backend"] or "unknown")
            by_route[f"{requested}->{selected}"] += 1
        elif status == "blocked" or bool(row["capacity_blocked"]):
            blocked += 1
        trigger = row["capacity_trigger"]
        if trigger and status in {"warning", "fallback", "blocked"}:
            by_trigger[str(trigger)] += 1
    return CapacityUsage(
        warnings=warnings,
        fallbacks=fallbacks,
        blocked=blocked,
        fallback_completed_turns=fallback_completed,
        fallback_duration_ms=fallback_duration_ms,
        by_route=dict(sorted(by_route.items())),
        by_trigger=dict(sorted(by_trigger.items())),
    )


def _calibration_summary(rows: list[Any]) -> CalibrationSummary:
    automatic = [row for row in rows if not bool(row["manual_override"])]
    labels: dict[str, int] = defaultdict(int)
    effort_sources: dict[str, int] = defaultdict(int)
    efforts: dict[str, int] = defaultdict(int)
    task_types: dict[str, int] = defaultdict(int)
    confidence_bands: dict[str, int] = defaultdict(int)
    override_signals = stronger_override_signals = 0
    tier_rank = {"fast": 0, "normal": 1, "smart": 2, "max": 3}
    for row in automatic:
        if row["outcome_label"]:
            labels[str(row["outcome_label"])] += 1
        effort_sources[str(row["reasoning_effort_source"] or "tier_default")] += 1
        efforts[str(row["reasoning_effort"] or "none")] += 1
        if row["classifier_task_type"]:
            task_types[normalize_task_type(str(row["classifier_task_type"])).value] += 1
        if row["classifier_confidence"] is not None:
            confidence = float(row["classifier_confidence"])
            band = (
                "<70%"
                if confidence < 0.70
                else "70-79%"
                if confidence < 0.80
                else "80-89%"
                if confidence < 0.90
                else "90-94%"
                if confidence < 0.95
                else "95-100%"
            )
            confidence_bands[band] += 1
        override = row["next_manual_override_tier"]
        if override:
            override_signals += 1
            if tier_rank.get(str(override), -1) > tier_rank.get(str(row["selected_tier"]), -1):
                stronger_override_signals += 1
    return CalibrationSummary(
        automatic_turns=len(automatic),
        labeled_turns=sum(labels.values()),
        explicit_correct=labels["correct"],
        too_low=labels["too-low"],
        too_high=labels["too-high"],
        execution_failed=labels["failed"],
        next_manual_override_signals=override_signals,
        stronger_next_override_signals=stronger_override_signals,
        reasoning_effort_sources=dict(sorted(effort_sources.items())),
        reasoning_efforts=dict(sorted(efforts.items())),
        task_types=dict(sorted(task_types.items(), key=lambda item: (-item[1], item[0]))),
        confidence_bands=dict(sorted(confidence_bands.items())),
    )


def _usage_summary(
    rows: list[Any], pricing: PricingConfig, baseline: str
) -> dict[str, int | float | None]:
    report = cost_report(rows, pricing, baseline)
    duration = _duration_summary(rows)
    return {
        "turns": len(rows),
        "completed_turns": duration.completed_turns,
        "measured_turns": report.measured_turns,
        "average_duration_ms": duration.average_ms,
        "p50_duration_ms": duration.p50_ms,
        "p95_duration_ms": duration.p95_ms,
        "maximum_duration_ms": duration.maximum_ms,
        "input_tokens": report.input_tokens,
        "cached_input_tokens": report.cached_input_tokens,
        "cache_write_input_tokens": report.cache_write_input_tokens,
        "output_tokens": report.output_tokens,
        "reasoning_output_tokens": report.reasoning_output_tokens,
        "total_tokens": sum(
            int(row["answer_total_tokens"] or 0)
            if row["usage_recorded_at"] is not None
            else 0
            for row in rows
        ),
        "answer_cost": report.actual_cost,
    }


def usage_analytics(
    rows: list[Any],
    pricing: PricingConfig,
    baseline: str,
    bucket: str = "day",
    longest: int = 10,
) -> AnalyticsReport:
    """Group answer and classifier token usage without retaining prompt contents."""
    if bucket not in {"day", "week", "month"}:
        raise ValueError("bucket must be day, week, or month")

    by_model_rows: dict[tuple[str, str], list[Any]] = defaultdict(list)
    by_period_rows: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    classifier_rows: dict[str, list[Any]] = defaultdict(list)
    application_states: dict[str, int] = defaultdict(int)
    for row in rows:
        backend = str(row["answer_backend"] or row["backend"] or "unknown")
        model = str(row["answer_model"] or row["model"] or "unknown")
        by_model_rows[(backend, model)].append(row)
        by_period_rows[(_period(str(row["created_at"]), bucket), backend, model)].append(row)
        application_states[str(row["route_application_state"] or "unknown")] += 1
        usage = _classifier_usage(row)
        if usage or row["classifier_latency_ms"] is not None:
            classifier_rows[_classifier_model(row)].append(row)

    def model_usage(backend: str, model: str, grouped_rows: list[Any]) -> ModelUsage:
        return ModelUsage(
            backend=backend,
            model=model,
            **_usage_summary(grouped_rows, pricing, baseline),
        )

    models = tuple(
        sorted(
            (
                model_usage(backend, model, grouped_rows)
                for (backend, model), grouped_rows in by_model_rows.items()
            ),
            key=lambda item: (-item.turns, item.backend, item.model),
        )
    )
    periods = tuple(
        sorted(
            (
                PeriodUsage(
                    period=period,
                    **asdict(model_usage(backend, model, grouped_rows)),
                )
                for (period, backend, model), grouped_rows in by_period_rows.items()
            ),
            key=lambda item: (item.period, item.backend, item.model),
        )
    )

    classifiers: list[ClassifierUsage] = []
    for model, grouped_rows in classifier_rows.items():
        prompt = cached = completion = 0
        latencies: list[float] = []
        statuses: list[str] = []
        failure_reasons: dict[str, int] = defaultdict(int)
        for row in grouped_rows:
            usage = _classifier_usage(row)
            prompt += int(usage.get("prompt_tokens", 0) or 0)
            details = usage.get("prompt_tokens_details", {})
            cached += int(details.get("cached_tokens", 0) or 0) if isinstance(details, dict) else 0
            completion += int(usage.get("completion_tokens", 0) or 0)
            if row["classifier_latency_ms"] is not None:
                latencies.append(float(row["classifier_latency_ms"]))
            status = str(row["classifier_status"] or "fallback")
            statuses.append(status)
            if status != "succeeded":
                reason = str(
                    row["classifier_error_type"]
                    or ("historical_unknown" if status == "fallback" else status)
                )
                failure_reasons[reason] += 1
        sorted_latencies = sorted(latencies)
        p95_index = max(0, math.ceil(len(sorted_latencies) * 0.95) - 1)
        calls = len(grouped_rows)
        successful_calls = statuses.count("succeeded")
        fallback_calls = calls - successful_calls
        timeout_calls = statuses.count("timeout")
        classifiers.append(
            ClassifierUsage(
                model=model,
                calls=calls,
                successful_calls=successful_calls,
                fallback_calls=fallback_calls,
                timeout_calls=timeout_calls,
                error_calls=statuses.count("error"),
                success_rate=successful_calls / calls,
                fallback_rate=fallback_calls / calls,
                timeout_rate=timeout_calls / calls,
                failure_reasons=dict(sorted(failure_reasons.items())),
                prompt_tokens=prompt,
                cached_input_tokens=cached,
                completion_tokens=completion,
                total_tokens=prompt + completion,
                average_latency_ms=sum(latencies) / len(latencies) if latencies else None,
                p50_latency_ms=float(median(sorted_latencies)) if sorted_latencies else None,
                p95_latency_ms=sorted_latencies[p95_index] if sorted_latencies else None,
                estimated_cost=token_cost(
                    model,
                    {
                        "input_tokens": prompt,
                        "cached_input_tokens": cached,
                        "output_tokens": completion,
                    },
                    pricing,
                ),
            )
        )
    longest_rows = sorted(
        ((row, duration) for row in rows if (duration := _duration_ms(row)) is not None),
        key=lambda item: (-item[1], int(item[0]["id"])),
    )[: max(0, longest)]
    return AnalyticsReport(
        rows=len(rows),
        model_mismatch_turns=sum(bool(row["answer_model_mismatch"]) for row in rows),
        route_application=dict(sorted(application_states.items())),
        overall=cost_report(rows, pricing, baseline),
        by_model=models,
        over_time=periods,
        classifiers=tuple(sorted(classifiers, key=lambda item: (-item.calls, item.model))),
        duration=_duration_summary(rows),
        reconciliation=_reconciliation_summary(rows),
        capacity=_capacity_summary(rows),
        calibration=_calibration_summary(rows),
        longest_turns=tuple(
            LongestTurn(
                decision_id=int(row["id"]),
                created_at=str(row["created_at"]),
                backend=str(row["answer_backend"] or row["backend"] or "unknown"),
                model=str(row["answer_model"] or row["model"] or "unknown"),
                tier=str(row["selected_tier"]),
                duration_ms=duration,
                total_tokens=int(row["answer_total_tokens"] or 0),
            )
            for row, duration in longest_rows
        ),
    )
