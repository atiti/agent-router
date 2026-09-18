"""Privacy-safe local aggregation of AgentRoute audit usage."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

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
    measured_turns: int
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
    prompt_tokens: int
    cached_input_tokens: int
    completion_tokens: int
    total_tokens: int
    average_latency_ms: float | None
    estimated_cost: float


@dataclass(frozen=True)
class AnalyticsReport:
    rows: int
    overall: CostReport
    by_model: tuple[ModelUsage, ...]
    over_time: tuple[PeriodUsage, ...]
    classifiers: tuple[ClassifierUsage, ...]


def _usage_summary(
    rows: list[Any], pricing: PricingConfig, baseline: str
) -> dict[str, int | float]:
    report = cost_report(rows, pricing, baseline)
    return {
        "turns": len(rows),
        "measured_turns": report.measured_turns,
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
    rows: list[Any], pricing: PricingConfig, baseline: str, bucket: str = "day"
) -> AnalyticsReport:
    """Group answer and classifier token usage without retaining prompt contents."""
    if bucket not in {"day", "week", "month"}:
        raise ValueError("bucket must be day, week, or month")

    by_model_rows: dict[tuple[str, str], list[Any]] = defaultdict(list)
    by_period_rows: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    classifier_rows: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        backend = str(row["backend"] or "unknown")
        model = str(row["answer_model"] or row["model"] or "unknown")
        by_model_rows[(backend, model)].append(row)
        by_period_rows[(_period(str(row["created_at"]), bucket), backend, model)].append(row)
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
        for row in grouped_rows:
            usage = _classifier_usage(row)
            prompt += int(usage.get("prompt_tokens", 0) or 0)
            details = usage.get("prompt_tokens_details", {})
            cached += int(details.get("cached_tokens", 0) or 0) if isinstance(details, dict) else 0
            completion += int(usage.get("completion_tokens", 0) or 0)
            if row["classifier_latency_ms"] is not None:
                latencies.append(float(row["classifier_latency_ms"]))
        classifiers.append(
            ClassifierUsage(
                model=model,
                calls=len(grouped_rows),
                prompt_tokens=prompt,
                cached_input_tokens=cached,
                completion_tokens=completion,
                total_tokens=prompt + completion,
                average_latency_ms=sum(latencies) / len(latencies) if latencies else None,
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
    return AnalyticsReport(
        rows=len(rows),
        overall=cost_report(rows, pricing, baseline),
        by_model=models,
        over_time=periods,
        classifiers=tuple(sorted(classifiers, key=lambda item: (-item.calls, item.model))),
    )
