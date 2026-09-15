from __future__ import annotations

import hashlib
import math

from .config import AppConfig, load_config
from .models import ReasonCode, RouteContext, RouteDecision, ScoreContribution, Tier
from .signals import extract_signals, is_confirmation, prompt_override, reason_codes


def tier_from_score(score: float) -> Tier:
    if score <= -1:
        return Tier.FAST
    if score < 3:
        return Tier.NORMAL
    if score < 5.5:
        return Tier.SMART
    return Tier.MAX


def confidence_for(score: float, tier: Tier, signal_count: int) -> float:
    boundaries = {-1.0, 3.0, 6.0}
    distance = min(abs(score - value) for value in boundaries)
    base = 0.60 + min(distance, 3.0) * 0.09 + min(signal_count, 4) * 0.025
    if tier in (Tier.FAST, Tier.MAX) and distance >= 2:
        base += 0.04
    return round(min(base, 0.99), 2)


class Router:
    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or load_config()

    def route(self, context: RouteContext) -> RouteDecision:
        provider = self.config.providers[context.provider]
        override, _ = prompt_override(context.latest_prompt)
        contributions = extract_signals(context)
        raw_score = sum(item.weight for item in contributions)
        inherited = False
        manual = override not in (None, "auto")

        if manual:
            proposed = Tier.parse(override or "normal")
            confidence = 1.0
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.MANUAL_OVERRIDE,
                    weight=0,
                    detail=f"explicit @{override} override",
                )
            )
        elif is_confirmation(context.latest_prompt) and context.previous_task_tier is not None:
            proposed = context.previous_task_tier
            confidence = 0.98
            inherited = True
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.PREVIOUS_TASK_INHERITANCE,
                    weight=0,
                    detail="confirmation inherited the previous task tier",
                )
            )
        else:
            proposed = tier_from_score(raw_score)
            confidence = confidence_for(raw_score, proposed, len(contributions))
            high_confidence_fast_codes = {
                ReasonCode.MECHANICAL_TASK,
                ReasonCode.READ_ONLY_RETRIEVAL,
                ReasonCode.SIMPLE_CONTEXT_QUESTION,
            }
            if proposed is Tier.FAST and any(
                item.code in high_confidence_fast_codes for item in contributions
            ):
                confidence = max(confidence, 0.90)

        # An explicit tier is an escape hatch and therefore bypasses automatic
        # risk floors and hysteresis. Policy max_tier still caps every route.
        if not manual:
            proposed = self._apply_risk_floor(proposed, context, contributions)
            proposed, confidence = self._apply_switching(
                proposed, confidence, context, contributions
            )
        max_tier = Tier.parse(self.config.policy.max_tier)
        if proposed > max_tier:
            proposed = max_tier
            contributions.append(
                ScoreContribution(code=ReasonCode.QUOTA_LIMIT, weight=0, detail="policy max tier")
            )

        target = provider.target(proposed)
        digest = hashlib.sha256(context.latest_prompt.encode("utf-8")).hexdigest()
        return RouteDecision(
            tier=proposed,
            model=target.model,
            reasoning_effort=target.reasoning_effort,
            confidence=confidence,
            raw_score=round(raw_score, 2),
            reason_codes=reason_codes(contributions),
            contributions=contributions,
            provider=context.provider,
            session_id=context.session_id,
            prompt_hash=digest,
            manual_override=manual,
            inherited=inherited,
            switched=proposed != context.current_tier,
        )

    def _apply_risk_floor(
        self,
        proposed: Tier,
        context: RouteContext,
        contributions: list[ScoreContribution],
    ) -> Tier:
        flags = set(context.task_risk_flags)
        codes = {item.code for item in contributions}
        if ReasonCode.SECURITY in codes:
            flags.add("security")
        if ReasonCode.MIGRATION in codes:
            flags.add("database_migration")
        floor = Tier.FAST
        matched: list[str] = []
        for flag in flags:
            configured = self.config.policy.risk_floors.get(flag)
            if configured:
                candidate = Tier.parse(configured)
                if candidate > floor:
                    floor = candidate
                matched.append(flag)
        if floor > proposed:
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.RISK_FLOOR,
                    weight=0,
                    detail=f"risk floor from {', '.join(sorted(matched))}",
                )
            )
            return floor
        return proposed

    def _apply_switching(
        self,
        proposed: Tier,
        confidence: float,
        context: RouteContext,
        contributions: list[ScoreContribution],
    ) -> tuple[Tier, float]:
        if proposed == context.current_tier:
            return proposed, confidence
        switching = self.config.routing.switching
        if proposed > context.current_tier and confidence < switching.upgrade_confidence:
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.SESSION_AFFINITY,
                    weight=-switching.switch_penalty,
                    detail="upgrade confidence below threshold",
                )
            )
            return context.current_tier, confidence
        if proposed < context.current_tier and confidence < switching.downgrade_confidence:
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.DOWNGRADE_HYSTERESIS,
                    weight=-switching.switch_penalty,
                    detail="kept current tier; downgrade requires stronger confidence",
                )
            )
            return context.current_tier, confidence
        jump = abs(int(proposed) - int(context.current_tier))
        if jump >= 3 and confidence < 0.9:
            proposed = Tier(
                int(context.current_tier) + (1 if proposed > context.current_tier else -1)
            )
            confidence = max(0.6, confidence - 0.05)
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.SESSION_AFFINITY,
                    weight=-switching.switch_penalty,
                    detail="limited an uncertain three-tier jump",
                )
            )
        return proposed, math.floor(confidence * 100) / 100
