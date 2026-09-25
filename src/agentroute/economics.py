"""Conservative next-request estimates, not promises of end-to-end savings."""

from __future__ import annotations

from .config import AppConfig
from .models import RouteContext, Tier
from .pricing import canonical_model


def cache_switch_estimate(
    config: AppConfig,
    context: RouteContext,
    proposed: Tier,
) -> dict:
    switching = config.routing.switching
    if switching.cache_economics == "off":
        return {}
    backend_name = config.routing.backend_by_tier.get(str(proposed), "gpt")
    backend = config.backends.get(backend_name)
    if (
        backend is None
        or not backend.enabled
        or config.routing.backend_by_tier.get(str(context.current_tier), "gpt") != backend_name
        or backend.codex_provider != context.current_model_provider
        or proposed >= context.current_tier
        or context.interrupted_turn_affinity
        or context.requested_backend
        or context.sticky_backend
        or context.inherited_backend
    ):
        return {"status": "ineligible"}
    current = context.current_model
    # Retaining a tier must resolve to the exact active model, not a similarly named alias.
    if backend.target(context.current_tier).model != current:
        return {"status": "unmapped_active_model"}
    candidate = backend.target(proposed).model
    if current == candidate:
        return {"status": "same_model"}
    usage = context.previous_response_usage
    inputs = usage.get("input_tokens", 0)
    cached = min(inputs, usage.get("cached_input_tokens", 0))
    outputs = usage.get("output_tokens", 0)
    if inputs <= 0 or cached / inputs < switching.cache_minimum_fraction:
        return {"status": "insufficient_cache_evidence"}
    current_price = config.pricing.models.get(canonical_model(current, config.pricing))
    candidate_price = config.pricing.models.get(canonical_model(candidate, config.pricing))
    if current_price is None or candidate_price is None:
        return {"status": "unpriced"}
    stay = (
        (inputs - cached) * current_price.input_per_million
        + cached * current_price.cached_input_per_million
        + outputs * current_price.output_per_million
    ) / 1_000_000
    switch = (
        inputs * candidate_price.input_per_million + outputs * candidate_price.output_per_million
    ) / 1_000_000
    prefer_stay = stay < switch * (1 - switching.cache_minimum_savings)
    return {
        "status": "estimated",
        "mode": switching.cache_economics,
        "backend": backend_name,
        "current_model": current,
        "candidate_model": candidate,
        "stay_estimate": stay,
        "switch_estimate": switch,
        "cached_input_fraction": cached / inputs,
        "prefer_stay": prefer_stay,
        # Subscription capacity cannot be converted into marginal API expenditure.
        "retain": prefer_stay and switching.cache_economics == "retain" and backend_name != "gpt",
        "basis": "API-equivalent estimate" if backend_name == "gpt" else "configured API prices",
        "assumptions": (
            "Next request repeats last response sizes; staying reuses cache; switching is cold."
        ),
    }
