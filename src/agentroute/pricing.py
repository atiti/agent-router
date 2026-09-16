from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .config import PricingConfig


@dataclass(frozen=True)
class CostReport:
    measured_turns: int
    unmeasured_turns: int
    actual_cost: float
    baseline_cost: float
    classifier_cost: float
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int

    @property
    def gross_savings(self) -> float:
        return self.baseline_cost - self.actual_cost

    @property
    def net_savings(self) -> float:
        return self.gross_savings - self.classifier_cost

    @property
    def savings_percent(self) -> float:
        return self.net_savings / self.baseline_cost * 100 if self.baseline_cost else 0.0


def canonical_model(model: str, pricing: PricingConfig) -> str:
    aliased = pricing.aliases.get(model, model)
    if aliased in pricing.models:
        return aliased
    suffix_matches = [name for name in pricing.models if aliased.endswith(name)]
    return max(suffix_matches, key=len) if suffix_matches else aliased


def token_cost(model: str, usage: dict[str, int | float], pricing: PricingConfig) -> float:
    rate = pricing.models.get(canonical_model(model, pricing))
    if rate is None:
        return 0.0
    input_tokens = max(0.0, float(usage.get("input_tokens", 0)))
    cached = max(0.0, float(usage.get("cached_input_tokens", 0)))
    cache_write = max(0.0, float(usage.get("cache_write_input_tokens", 0)))
    uncached = max(0.0, input_tokens - cached - cache_write)
    output = max(0.0, float(usage.get("output_tokens", 0)))
    write_rate = rate.cache_write_per_million or rate.input_per_million
    return (
        uncached * rate.input_per_million
        + cached * rate.cached_input_per_million
        + cache_write * write_rate
        + output * rate.output_per_million
    ) / 1_000_000


def cost_report(rows: list[sqlite3.Row], pricing: PricingConfig, baseline: str) -> CostReport:
    measured = 0
    actual = baseline_cost = classifier_cost = 0.0
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    for row in rows:
        if row["usage_recorded_at"] is None:
            continue
        measured += 1
        classifier_usage = json.loads(row["classifier_usage"] or "{}")
        receipt = json.loads(row["selection_receipt"] or "{}")
        classifier = receipt.get("classifier", {}) if isinstance(receipt, dict) else {}
        classifier_model = classifier.get("model", "") if isinstance(classifier, dict) else ""
        classifier_cost += token_cost(
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
        usage = {
            "input_tokens": row["answer_input_tokens"] or 0,
            "cached_input_tokens": row["answer_cached_input_tokens"] or 0,
            "cache_write_input_tokens": row["answer_cache_write_input_tokens"] or 0,
            "output_tokens": row["answer_output_tokens"] or 0,
        }
        for name in totals:
            totals[name] += int(row[f"answer_{name}"] or 0)
        actual += token_cost(str(row["answer_model"] or row["model"]), usage, pricing)
        baseline_cost += token_cost(baseline, usage, pricing)
    return CostReport(
        measured,
        len(rows) - measured,
        actual,
        baseline_cost,
        classifier_cost,
        **totals,
    )
