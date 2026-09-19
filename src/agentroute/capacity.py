from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import AppConfig
from .pricing import token_cost


@dataclass(frozen=True)
class CapacityState:
    backend: str
    status: str
    detail: str
    trigger: str | None = None
    used_percent: float | None = None
    resets_at: int | None = None
    account_id: str | None = None

    @property
    def available(self) -> bool:
        return self.status not in {"exhausted", "budget_reached", "unavailable"}


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def subscription_state(
    config: AppConfig,
    snapshot: dict[str, object] | None,
    account_id: str | None = None,
    *,
    recovery_hold: bool = False,
) -> CapacityState:
    if not config.capacity.enabled:
        return CapacityState("gpt", "disabled", "capacity management disabled")
    data = snapshot or {}
    ordinary_allowed = data.get("ordinaryUsageAllowed")
    if ordinary_allowed is None:
        ordinary_allowed = data.get("ordinary_usage_allowed")
    nested = data.get("rateLimits") or data.get("rate_limits")
    if isinstance(nested, dict):
        data = nested
    reached = data.get("rateLimitReachedType") or data.get("rate_limit_reached_type")
    spend_reached = data.get("spendControlReached")
    if spend_reached is None:
        spend_reached = data.get("spend_control_reached")
    windows = [data.get("primary"), data.get("secondary")]
    parsed: list[tuple[float, int | None]] = []
    for item in windows:
        if not isinstance(item, dict):
            continue
        used = _number(item.get("usedPercent", item.get("used_percent")))
        reset = item.get("resetsAt", item.get("resets_at"))
        if used is not None:
            parsed.append((used, int(reset) if reset is not None else None))
    used_percent, resets_at = max(parsed, default=(None, None), key=lambda item: item[0] or 0)
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    reset_passed = resets_at is not None and resets_at <= now_epoch
    recovery_threshold = max(
        0.0, config.capacity.switch_percent - config.capacity.recovery_margin_percent
    )
    if ordinary_allowed is False or reached or spend_reached is True or (
        used_percent is not None and used_percent >= config.capacity.switch_percent
    ):
        reason = str(reached or "subscription usage unavailable")
        return CapacityState(
            "gpt", "exhausted", reason, "subscription", used_percent, resets_at, account_id
        )
    if (
        recovery_hold
        and not reset_passed
        and used_percent is not None
        and used_percent >= recovery_threshold
    ):
        return CapacityState(
            "gpt",
            "exhausted",
            f"subscription recovery hold until below {recovery_threshold:g}% or reset",
            "subscription_recovery",
            used_percent,
            resets_at,
            account_id,
        )
    if used_percent is not None and used_percent >= config.capacity.warn_percent:
        return CapacityState(
            "gpt", "warning", f"subscription {used_percent:g}% used",
            "subscription", used_percent, resets_at, account_id
        )
    if used_percent is None:
        return CapacityState(
            "gpt",
            "unknown",
            "subscription telemetry unavailable",
            account_id=account_id,
        )
    return CapacityState(
        "gpt", "healthy", f"subscription {used_percent:g}% used",
        "subscription", used_percent, resets_at, account_id
    )


def backend_state(
    config: AppConfig,
    backend_name: str,
    *,
    rate_limits: dict[str, object] | None = None,
    account_id: str | None = None,
    daily_spend: float = 0,
    monthly_spend: float = 0,
    recovery_hold: bool = False,
) -> CapacityState:
    if backend_name == "gpt":
        return subscription_state(
            config, rate_limits, account_id, recovery_hold=recovery_hold
        )
    if not config.capacity.enabled:
        return CapacityState(backend_name, "disabled", "capacity management disabled")
    backend = config.backends[backend_name]
    if backend.daily_budget_usd is not None and daily_spend >= backend.daily_budget_usd:
        return CapacityState(
            backend_name, "budget_reached",
            f"daily budget ${backend.daily_budget_usd:g} reached (${daily_spend:.2f} used)",
            "daily_budget",
        )
    if backend.monthly_budget_usd is not None and monthly_spend >= backend.monthly_budget_usd:
        return CapacityState(
            backend_name, "budget_reached",
            f"monthly budget ${backend.monthly_budget_usd:g} reached (${monthly_spend:.2f} used)",
            "monthly_budget",
        )
    ratios = [
        daily_spend / backend.daily_budget_usd
        if backend.daily_budget_usd else 0,
        monthly_spend / backend.monthly_budget_usd
        if backend.monthly_budget_usd else 0,
    ]
    recovery_threshold = max(
        0.0, config.capacity.switch_percent - config.capacity.recovery_margin_percent
    )
    if recovery_hold and max(ratios) * 100 >= recovery_threshold:
        return CapacityState(
            backend_name,
            "budget_reached",
            f"API budget recovery hold until below {recovery_threshold:g}%",
            "api_budget_recovery",
        )
    if max(ratios) * 100 >= config.capacity.warn_percent:
        return CapacityState(
            backend_name, "warning",
            f"API budget {max(ratios) * 100:.0f}% used", "api_budget"
        )
    return CapacityState(
        backend_name, "healthy",
        f"today ${daily_spend:.2f}; month ${monthly_spend:.2f}", "api_budget"
    )


def backend_spend(rows: list[Any], config: AppConfig) -> tuple[dict[str, float], dict[str, float]]:
    """Calculate measured API-equivalent spend by backend for today and this month."""
    now = datetime.now(timezone.utc)
    daily: dict[str, float] = {}
    monthly: dict[str, float] = {}
    for row in rows:
        if row["usage_recorded_at"] is None:
            continue
        try:
            created = datetime.fromisoformat(str(row["created_at"])).astimezone(timezone.utc)
        except ValueError:
            continue
        backend = str(row["backend"] or "unknown")
        model = str(row["answer_model"] or row["model"] or "unknown")
        usage = {
            "input_tokens": int(row["answer_input_tokens"] or 0),
            "cached_input_tokens": int(row["answer_cached_input_tokens"] or 0),
            "cache_write_input_tokens": int(row["answer_cache_write_input_tokens"] or 0),
            "output_tokens": int(row["answer_output_tokens"] or 0),
        }
        cost = token_cost(model, usage, config.pricing)
        if created.year == now.year and created.month == now.month:
            monthly[backend] = monthly.get(backend, 0.0) + cost
            if created.date() == now.date():
                daily[backend] = daily.get(backend, 0.0) + cost
    return daily, monthly


def fallback_chain(config: AppConfig, backend_name: str) -> list[str]:
    """Return a bounded, cycle-safe fallback chain."""
    chain: list[str] = []
    current = backend_name
    for _ in range(len(config.backends)):
        backend = config.backends.get(current)
        candidate = (
            backend.fallback_backend if backend else None
        ) or config.capacity.default_fallback_backend
        if not candidate or candidate in chain or candidate == backend_name:
            break
        chain.append(candidate)
        current = candidate
    return chain


def fallback_cycle(config: AppConfig, backend_name: str) -> list[str]:
    """Return the repeated fallback path when a configured cycle exists."""
    seen = [backend_name]
    current = backend_name
    for _ in range(len(config.backends) + 1):
        backend = config.backends.get(current)
        candidate = (
            backend.fallback_backend if backend else None
        ) or config.capacity.default_fallback_backend
        if not candidate:
            return []
        if candidate in seen:
            return [*seen[seen.index(candidate):], candidate]
        seen.append(candidate)
        current = candidate
    return seen


def reset_description(resets_at: int | None) -> str | None:
    if resets_at is None:
        return None
    reset = datetime.fromtimestamp(resets_at, timezone.utc)
    return reset.strftime("%Y-%m-%d %H:%M UTC")
