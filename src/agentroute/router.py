from __future__ import annotations

import hashlib
import math

from .config import AppConfig, load_config
from .models import ReasonCode, RouteContext, RouteDecision, ScoreContribution, Tier
from .signals import (
    extract_signals,
    is_confirmation,
    is_context_followup,
    prompt_override,
    reason_codes,
)

CLASSIFIER_VERSION = "heuristic-v3"


def tier_from_score(score: float) -> Tier:
    if score <= -1:
        return Tier.FAST
    if score < 3:
        return Tier.NORMAL
    if score < 5.5:
        return Tier.SMART
    return Tier.MAX


def confidence_for(score: float, tier: Tier, signal_count: int) -> float:
    boundaries = {-1.0, 3.0, 5.5}
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
        task_context_used = False
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
        elif is_confirmation(context.latest_prompt) and context.agent_requested_tier is not None:
            inherited = True
            task_context_used = context.task_definition is not None
            if context.task_definition:
                task_context = context.model_copy(
                    update={"latest_prompt": context.task_definition, "task_definition": None}
                )
                contributions = extract_signals(task_context)
                raw_score = sum(item.weight for item in contributions)
            proposed = context.agent_requested_tier
            confidence = 0.99
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.AGENT_ESCALATION,
                    weight=0,
                    detail=f"user approved assistant request for {proposed}",
                )
            )
        elif is_confirmation(context.latest_prompt) or is_context_followup(context.latest_prompt):
            inherited = True
            if context.task_definition:
                task_context = context.model_copy(
                    update={"latest_prompt": context.task_definition, "task_definition": None}
                )
                contributions = extract_signals(task_context)
                raw_score = sum(item.weight for item in contributions)
                proposed, confidence = self._score(raw_score, contributions)
                task_context_used = True
                contributions.append(
                    ScoreContribution(
                        code=ReasonCode.TASK_DEFINITION_INHERITANCE,
                        weight=0,
                        detail=(
                            "continuation classified from the previous assistant task definition"
                        ),
                    )
                )
            elif context.previous_task_tier is not None:
                proposed = context.previous_task_tier
                confidence = 0.98
                contributions.append(
                    ScoreContribution(
                        code=ReasonCode.PREVIOUS_TASK_INHERITANCE,
                        weight=0,
                        detail="continuation inherited the previous selected tier",
                    )
                )
            else:
                proposed, confidence = self._score(raw_score, contributions)
        else:
            proposed, confidence = self._score(raw_score, contributions)

        score_proposed = proposed

        # An explicit tier is an escape hatch and therefore bypasses automatic
        # risk floors and hysteresis. Policy max_tier still caps every route.
        risk_floor_applied = False
        if not manual:
            proposed, risk_floor_applied = self._apply_risk_floor(
                proposed, context, contributions
            )
            if risk_floor_applied:
                confidence = max(confidence, 0.90)
            proposed, confidence = self._apply_switching(
                proposed, confidence, context, contributions
            )
            if proposed is Tier.MAX and context.current_tier is not Tier.MAX:
                proposed = Tier.SMART
                contributions.append(
                    ScoreContribution(
                        code=ReasonCode.MODEL_COMPATIBILITY_FALLBACK,
                        weight=0,
                        detail=(
                            "automatic MAX transition is incompatible with admitted Codex "
                            "Node REPL safety metadata; used SMART"
                        ),
                    )
                )
        max_tier = Tier.parse(self.config.policy.max_tier)
        if proposed > max_tier:
            proposed = max_tier
            contributions.append(
                ScoreContribution(code=ReasonCode.QUOTA_LIMIT, weight=0, detail="policy max tier")
            )

        target = provider.target(proposed)
        digest = hashlib.sha256(context.latest_prompt.encode("utf-8")).hexdigest()
        comparison_tier = context.previous_task_tier or context.current_tier
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
            switched=proposed != comparison_tier,
            proposed_tier=score_proposed,
            comparison_tier=comparison_tier,
            classifier_version=CLASSIFIER_VERSION,
            task_context_used=task_context_used,
            risk_floor_applied=risk_floor_applied,
            agent_requested_tier=context.agent_requested_tier,
            agent_request_reason_hash=context.agent_request_reason_hash,
        )

    @staticmethod
    def _score(
        raw_score: float, contributions: list[ScoreContribution]
    ) -> tuple[Tier, float]:
        proposed = tier_from_score(raw_score)
        confidence = confidence_for(raw_score, proposed, len(contributions))
        codes = {item.code for item in contributions}
        high_confidence_fast_codes = {
            ReasonCode.MECHANICAL_TASK,
            ReasonCode.READ_ONLY_RETRIEVAL,
            ReasonCode.SIMPLE_CONTEXT_QUESTION,
            ReasonCode.READ_ONLY_STATUS,
        }
        if proposed is Tier.FAST and codes & high_confidence_fast_codes:
            confidence = max(confidence, 0.90)
        if codes & {ReasonCode.CREDENTIAL_EXPOSURE, ReasonCode.OPERATIONAL_INCIDENT}:
            confidence = max(confidence, 0.90)
        if codes <= {ReasonCode.SMALL_SCOPE}:
            confidence = min(confidence, 0.55)
        return proposed, confidence

    def _apply_risk_floor(
        self,
        proposed: Tier,
        context: RouteContext,
        contributions: list[ScoreContribution],
    ) -> tuple[Tier, bool]:
        flags = set(context.task_risk_flags)
        codes = {item.code for item in contributions}
        if ReasonCode.SECURITY in codes:
            flags.add("security")
        if ReasonCode.MIGRATION in codes:
            flags.add("database_migration")
        if ReasonCode.CREDENTIAL_EXPOSURE in codes:
            flags.add("security")
        if ReasonCode.OPERATIONAL_INCIDENT in codes:
            flags.add("production")
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
            return floor, True
        return proposed, False

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
