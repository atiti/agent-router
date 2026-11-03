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
    incomplete_pricing_turns: int = 0

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


def turn_cost(row: sqlite3.Row, model: str, pricing: PricingConfig) -> float | None:
    """Price known requests or a provably flat aggregate; unknown tiers stay unknown."""
    rate = pricing.models.get(canonical_model(model, pricing))
    if rate is None:
        return None
    keys = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")
    usage = {key: int(row[f"answer_{key}"] or 0) for key in keys}
    try:
        receipt = json.loads(row["execution_receipt"] or "{}")
    except (KeyError, IndexError, TypeError, ValueError):
        receipt = {}
    if not isinstance(receipt, dict):
        receipt = {}
    if receipt.get("request_usage_version") == 1:
        requests = receipt.get("request_usage")
        if (
            receipt.get("coverage") != "turn_start_seen"
            or not isinstance(requests, list) or not requests
            or len(requests) != receipt.get("response_count")
        ):
            return None
        observed = {key: 0 for key in keys}
        response_ids: set[str] = set()
        costs = []
        for request in requests:
            if not isinstance(request, dict):
                return None
            response_id, counts = request.get("response_id"), request.get("usage")
            if (
                not isinstance(response_id, str) or not response_id
                or response_id in response_ids or not isinstance(counts, dict)
                or any(
                    not isinstance(counts.get(key), int) or isinstance(counts[key], bool)
                    or counts[key] < 0 for key in keys
                )
                or counts["cached_input_tokens"] + counts["cache_write_input_tokens"]
                > counts["input_tokens"]
            ):
                return None
            response_ids.add(response_id)
            for key in keys:
                observed[key] += counts[key]
            costs.append(token_cost(model, counts, pricing))
        return sum(costs) if observed == usage else None
    threshold = rate.long_context_threshold_tokens
    if (
        threshold is not None
        and long_context_multipliers(rate, usage["input_tokens"]) != (1.0, 1.0)
    ):
        # A cumulative total crossing a threshold does not prove any request did.
        return None
    return token_cost(model, usage, pricing)


def cost_report(rows: list[sqlite3.Row], pricing: PricingConfig, baseline: str) -> CostReport:
    measured = priced = incomplete = 0
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
        for name in totals:
            totals[name] += int(row[f"answer_{name}"] or 0)
        answer_model = str(row["answer_model"] or row["model"])
        if canonical_model(answer_model, pricing) not in pricing.models:
            unpriced_models.add(answer_model)
            continue
        row_cost = turn_cost(row, answer_model, pricing)
        row_baseline = turn_cost(row, baseline, pricing)
        if row_cost is None or row_baseline is None:
            incomplete += 1
            continue
        priced += 1
        comparison_classifier_cost += row_classifier_cost
        actual += row_cost
        baseline_cost += row_baseline
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
        incomplete_pricing_turns=incomplete,
    )
