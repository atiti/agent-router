"""Bounded, explainable provider selection after task-tier classification.

This policy never asks an LLM to invent provider availability or quota. It uses
configured task preferences and measured capacity, with explicit exclusions and
a switching penalty. It does not send prompts to candidate providers.
"""

from __future__ import annotations

import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, replace

from .capacity import CapacityState, backend_state
from .config import AppConfig, model_capabilities
from .models import ReasonCode, RouteContext, Tier
from .pricing import canonical_model
from .profiles import TurnProfileSelection
from .providers import backend_readiness


def _claude_capacity(config: AppConfig, state: CapacityState) -> CapacityState:
    # Identity-bound local snapshots only: no Keychain, OAuth refresh or usage API.
    from .claude_profile_pool import ClaudeProfilePool
    from .claude_profiles import profile_account_identity

    profiles = config.claude_subscriptions
    names = (
        [name for name, profile in profiles.profiles.items() if profile.enabled]
        if profiles.auto_select
        else [profiles.active_profile]
    )
    samples = []
    for name in names:
        profile = profiles.profiles.get(name)
        if profile is None or not profile.enabled:
            continue
        identity = profile_account_identity(profile)
        used = (
            ClaudeProfilePool._recorded_percent(name, identity, profile.auth_generation)
            if identity
            else None
        )
        samples.append(used)
    known = [sample for sample in samples if sample is not None]
    if not known or (None in samples and min(known) >= config.capacity.switch_percent):
        return replace(state, status="unknown", detail="Claude subscription telemetry unavailable")
    used = min(known)
    status = (
        "exhausted"
        if used >= config.capacity.switch_percent
        else "warning"
        if used >= config.capacity.warn_percent
        else "healthy"
    )
    return replace(
        state,
        status=status,
        used_percent=used,
        trigger="subscription",
        detail=f"Claude subscription {used:g}% used (fresh identity-bound local sample)",
    )


def candidate_capacity(config: AppConfig, context: RouteContext, name: str) -> CapacityState:
    state = backend_state(
        config,
        name,
        rate_limits=context.rate_limits,
        account_id=context.account_id,
        daily_spend=context.backend_daily_spend.get(name, 0),
        monthly_spend=context.backend_monthly_spend.get(name, 0),
        recovery_hold=(
            context.previous_capacity_backend == name
            and context.previous_capacity_status in {"fallback", "blocked", "exhausted"}
        ),
        probe=False,
    )
    if name == "claude" and state.available and config.capacity.enabled:
        return _claude_capacity(config, state)
    return state


def local_bridge_ready(config: AppConfig, name: str) -> bool:
    """Only check known local bridges; never probe arbitrary remote providers."""
    if name not in {"claude", "grok"}:
        return True
    url = urllib.parse.urlparse(config.backends[name].base_url or "")
    if url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return True
    try:
        with urllib.request.urlopen(
            f"{url.scheme}://{url.netloc}/healthz", timeout=0.25
        ) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def inferred_task_type(context: RouteContext, codes: list[ReasonCode]) -> str:
    if context.coordination_role == "supervisor":
        return "orchestration"
    for code, task in (
        (ReasonCode.SECURITY, "security"),
        (ReasonCode.CREDENTIAL_EXPOSURE, "security"),
        (ReasonCode.ARCHITECTURE, "architecture"),
        (ReasonCode.OPERATIONAL_INCIDENT, "operations"),
        (ReasonCode.DEBUGGING, "debugging"),
        (ReasonCode.MECHANICAL_TASK, "formatting"),
        (ReasonCode.BOUNDED_COMMUNICATION, "communication"),
        (ReasonCode.READ_ONLY_RETRIEVAL, "retrieval"),
    ):
        if code in codes:
            return task
    return "other"


def select_provider(
    config: AppConfig,
    context: RouteContext,
    tier: Tier,
    task_type: str,
    default_backend: str,
    *,
    locked_by: str | None = None,
    gpt_profile_selector: Callable[[Tier], TurnProfileSelection | None] | None = None,
    risk_codes: list[ReasonCode] = (),
    enforce_risk_floors: bool = True,
) -> tuple[str | None, dict[str, object]]:
    policy = config.routing.provider_selection
    receipt: dict[str, object] = {
        "version": "provider-selection-v1",
        "mode": policy.mode,
        "task_type": task_type,
        "default_backend": default_backend,
        "locked_by": locked_by,
    }
    if policy.mode == "static" or locked_by:
        receipt.update(selected_backend=default_backend, candidates=[])
        return default_backend, receipt

    preference = policy.task_preferences.get(task_type, [])
    rows: list[dict[str, object]] = []
    # The allowlist is an admission boundary; never add a backend through fallback.
    names = list(dict.fromkeys(policy.candidates))
    required_context = context.previous_response_usage.get("input_tokens", 0)
    flags = set(context.task_risk_flags)
    for code, flag in (
        (ReasonCode.SECURITY, "security"),
        (ReasonCode.CREDENTIAL_EXPOSURE, "security"),
        (ReasonCode.MIGRATION, "database_migration"),
        (ReasonCode.OPERATIONAL_INCIDENT, "production"),
    ):
        if code in risk_codes:
            flags.add(flag)
    if task_type == "security":
        flags.add("security")
    for name in names:
        backend = config.backends.get(name)
        exclusions: list[str] = []
        row: dict[str, object] = {"backend": name, "exclusions": exclusions}
        rows.append(row)
        if backend is None:
            exclusions.append("not_configured")
            row["eligible"] = False
            continue
        floors = config.policy.backend_risk_floors.get(name, {})
        required_tier = max(
            (
                Tier.parse(floors.get(flag, config.policy.risk_floors.get(flag, "fast")))
                for flag in flags
            ),
            default=Tier.FAST,
        )
        if enforce_risk_floors and tier < required_tier:
            exclusions.append(f"backend_risk_floor:{required_tier}")
        ready, problems = backend_readiness(config, name)
        if not ready:
            exclusions.extend(problems)
        target = backend.tiers.get(str(tier))
        if target is None:
            exclusions.append("tier_not_configured")
            row["eligible"] = False
            continue
        state = candidate_capacity(config, context, name)
        profile_name = config.capacity.active_profile
        # Resolve the account before comparing its model/capacity. Session account
        # affinity may use different targets from the configured default account.
        # The hook supplies its existing catalog-validated account chooser.
        if name == "gpt" and gpt_profile_selector and config.capacity.profiles:
            selection = gpt_profile_selector(tier)
            if selection and selection.selected:
                state = selection.selected.capacity
                profile_name = selection.selected.name
                row["subscription_profile"] = profile_name
                row["profile_capacity"] = asdict(state) | {"account_id": None}
            elif selection:
                state = CapacityState("gpt", "unavailable", "No compatible ChatGPT account")
        profile = config.capacity.profiles.get(profile_name or "")
        if name == "gpt" and profile:
            target = profile.target(tier, target)
        caps = model_capabilities(config, name, target.model)
        window = target.context_window or caps.context_window
        same_model = (
            backend.codex_provider == context.current_model_provider
            and target.model == context.current_model
        )
        if caps.tool_calling == "none":
            exclusions.append("tool_calling_unavailable")
        # Known-small windows cannot accept the already measured input. Unknown
        # windows are reported, never represented as a verified fit.
        if window and required_context > window:
            exclusions.append("context_window_too_small")
        if required_context and window is None and not same_model:
            exclusions.append("context_window_unverified_for_switch")
        if ready and not local_bridge_ready(config, name):
            exclusions.append("local_bridge_unavailable")
        if not state.available:
            exclusions.append(state.status)
        price_name = caps.pricing_model or canonical_model(target.model, config.pricing)
        price = config.pricing.models.get(price_name)
        # A small, explicitly labeled API-equivalent proxy, not subscription cash
        # spend or a prediction of the eventual token count.
        cost = (
            (price.input_per_million * 4000 + price.output_per_million * 1000) / 1e6
            if price
            else None
        )
        suitability = (
            max(0, 4 - preference.index(name))
            if name in preference
            else 2
            if name == default_backend
            else 1
        )
        usage_penalty = (
            policy.usage_weight * (state.used_percent / 100) ** 2
            if state.used_percent is not None
            else policy.unknown_capacity_penalty
            if state.status == "unknown"
            else 0
        )
        current = backend.codex_provider == context.current_model_provider
        switch_penalty = policy.switch_penalty if context.current_model and not current else 0
        cost_penalty = (
            policy.cost_weight * math.log1p(cost * 100) if cost is not None else policy.cost_weight
        )
        # Budget headroom influences ranking before the hard budget guard fires.
        budget_fraction = max(
            context.backend_daily_spend.get(name, 0) / backend.daily_budget_usd
            if backend.daily_budget_usd
            else 0,
            context.backend_monthly_spend.get(name, 0) / backend.monthly_budget_usd
            if backend.monthly_budget_usd
            else 0,
        )
        score = suitability - usage_penalty - switch_penalty - cost_penalty - budget_fraction
        row.update(
            model=target.model,
            eligible=not exclusions,
            score=round(score, 6),
            suitability=suitability,
            usage_penalty=round(usage_penalty, 6),
            switch_penalty=switch_penalty,
            cost_penalty=round(cost_penalty, 6),
            api_equivalent_cost_proxy_usd=cost,
            budget_fraction=round(budget_fraction, 6),
            capacity_status=state.status,
            capacity_detail=state.detail,
            used_percent=state.used_percent,
            context_window=window,
            measured_prior_input_tokens=required_context,
            prior_input_fits=bool(required_context and window and required_context <= window),
        )
    eligible = [row for row in rows if row["eligible"]]
    selected = (
        max(
            eligible,
            key=lambda row: (
                row["score"],
                config.backends[row["backend"]].codex_provider == context.current_model_provider
                if context.current_model
                else False,
            ),
        )["backend"]
        if eligible
        else None
    )
    receipt.update(
        candidates=rows,
        recommended_backend=selected,
        selected_backend=selected if policy.mode == "adaptive" else default_backend,
        observed_at=int(time.time()),
        capacity_probes="local_snapshots_and_existing_GPT_account_failover",
    )
    return (selected if policy.mode == "adaptive" else default_backend), receipt
