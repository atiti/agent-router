from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .config import ModelPrice, PricingConfig


@dataclass(frozen=True)
class CostReport:
    measured_turns: int
    unmeasured_turns: int
    priced_turns: int
    actual_cost: float
    baseline_cost: float
    classifier_cost: float
    comparison_classifier_cost: float
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    unpriced_models: tuple[str, ...]

    @property
    def gross_savings(self) -> float | None:
        return self.baseline_cost - self.actual_cost if self.priced_turns else None

    @property
    def net_savings(self) -> float | None:
        gross = self.gross_savings
        return gross - self.comparison_classifier_cost if gross is not None else None

    @property
    def savings_percent(self) -> float | None:
        net = self.net_savings
        return net / self.baseline_cost * 100 if net is not None and self.baseline_cost else None


def canonical_model(model: str, pricing: PricingConfig) -> str:
    aliased = pricing.aliases.get(model, model)
    if aliased in pricing.models:
        return aliased
    suffix_matches = [name for name in pricing.models if aliased.endswith(name)]
    return max(suffix_matches, key=len) if suffix_matches else aliased


def long_context_multipliers(rate: ModelPrice, input_tokens: float) -> tuple[float, float]:
    threshold = rate.long_context_threshold_tokens
    if threshold is None or input_tokens < threshold or (
        input_tokens == threshold and not rate.long_context_threshold_inclusive
    ):
        return 1.0, 1.0
    output = rate.long_context_output_multiplier
    return rate.long_context_multiplier, (
        rate.long_context_multiplier if output is None else output
    )


def token_cost(model: str, usage: dict[str, int | float], pricing: PricingConfig) -> float:
    """Price one request; long-context thresholds never apply to a sum of requests."""
    rate = pricing.models.get(canonical_model(model, pricing))
    if rate is None:
        return 0.0
    input_tokens = max(0.0, float(usage.get("input_tokens", 0)))
    cached = max(0.0, float(usage.get("cached_input_tokens", 0)))
    cache_write = max(0.0, float(usage.get("cache_write_input_tokens", 0)))
    uncached = max(0.0, input_tokens - cached - cache_write)
    output = max(0.0, float(usage.get("output_tokens", 0)))
    write_rate = (
        rate.input_per_million
        if rate.cache_write_per_million is None else rate.cache_write_per_million
    )
    input_multiplier, output_multiplier = long_context_multipliers(rate, input_tokens)
    return (
        (uncached * rate.input_per_million
        + cached * rate.cached_input_per_million
        + cache_write * write_rate) * input_multiplier
        + output * rate.output_per_million * output_multiplier
    ) / 1_000_000


def cost_report(rows: list[sqlite3.Row], pricing: PricingConfig, baseline: str) -> CostReport:
    measured = priced = 0
    actual = baseline_cost = classifier_cost = comparison_classifier_cost = 0.0
    unpriced_models: set[str] = set()
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    for row in rows:
        classifier_usage = json.loads(row["classifier_usage"] or "{}")
        receipt = json.loads(row["selection_receipt"] or "{}")
        classifier = receipt.get("classifier", {}) if isinstance(receipt, dict) else {}
        classifier_model = classifier.get("model", "") if isinstance(classifier, dict) else ""
        if (
            classifier_model
            and canonical_model(str(classifier_model), pricing) not in pricing.models
        ):
            unpriced_models.add(str(classifier_model))
        row_classifier_cost = token_cost(
            str(classifier_model),
            {
                "input_tokens": classifier_usage.get("prompt_tokens", 0),
                "cached_input_tokens": classifier_usage.get("prompt_tokens_details", {}).get(
                    "cached_tokens", 0
                )
                if isinstance(classifier_usage.get("prompt_tokens_details"), dict)
                else 0,
                "output_tokens": classifier_usage.get("completion_tokens", 0),
            },
            pricing,
        )
        classifier_cost += row_classifier_cost
        if row["usage_recorded_at"] is None:
            continue
        measured += 1
        usage = {
            "input_tokens": row["answer_input_tokens"] or 0,
            "cached_input_tokens": row["answer_cached_input_tokens"] or 0,
            "cache_write_input_tokens": row["answer_cache_write_input_tokens"] or 0,
            "output_tokens": row["answer_output_tokens"] or 0,
        }
        for name in totals:
            totals[name] += int(row[f"answer_{name}"] or 0)
        answer_model = str(row["answer_model"] or row["model"])
        if canonical_model(answer_model, pricing) not in pricing.models:
            unpriced_models.add(answer_model)
            continue
        priced += 1
        comparison_classifier_cost += row_classifier_cost
        actual += token_cost(answer_model, usage, pricing)
        baseline_cost += token_cost(baseline, usage, pricing)
    return CostReport(
        measured,
        len(rows) - measured,
        priced,
        actual,
        baseline_cost,
        classifier_cost,
        comparison_classifier_cost,
        **totals,
        unpriced_models=tuple(sorted(unpriced_models)),
    )
