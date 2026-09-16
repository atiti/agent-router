from __future__ import annotations

import hashlib
import json
import math

from .classifier import OpenAICompatibleClassifier, TierClassifier, catalog_age_seconds
from .config import AppConfig, load_config
from .models import ReasonCode, RouteContext, RouteDecision, ScoreContribution, Tier
from .providers import backend_readiness
from .signals import (
    contains_credential,
    continues_previous_task,
    extract_signals,
    is_confirmation,
    reason_codes,
    route_overrides,
)

CLASSIFIER_VERSION = "hybrid-v7"


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
    def __init__(
        self,
        config: AppConfig | None = None,
        classifier: TierClassifier | None = None,
    ) -> None:
        self.config = config or load_config()
        classifier_config = self.config.routing.classifier
        self.classifier = classifier
        if self.classifier is None and classifier_config.enabled:
            self.classifier = OpenAICompatibleClassifier(classifier_config)

    def route(self, context: RouteContext) -> RouteDecision:
        override, backend_override, _ = route_overrides(context.latest_prompt)
        contributions = extract_signals(context)
        raw_score = sum(item.weight for item in contributions)
        inherited = False
        task_context_used = False
        manual = override not in (None, "auto")
        classification_source = "heuristic"
        classifier_confidence: float | None = None
        classifier_task_type: str | None = None
        classifier_reason_hash: str | None = None

        if manual:
            proposed = Tier.parse(override or "normal")
            confidence = 1.0
            classification_source = "manual"
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
            classification_source = "agent_request"
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.AGENT_ESCALATION,
                    weight=0,
                    detail=f"user approved assistant request for {proposed}",
                )
            )
        elif continues_previous_task(context.latest_prompt):
            inherited = True
            if context.task_definition:
                task_context = context.model_copy(
                    update={"latest_prompt": context.task_definition, "task_definition": None}
                )
                contributions = extract_signals(task_context)
                raw_score = sum(item.weight for item in contributions)
                proposed, confidence = self._score(raw_score, contributions)
                (
                    proposed,
                    confidence,
                    classification_source,
                    classifier_confidence,
                    classifier_task_type,
                    classifier_reason_hash,
                ) = self._maybe_classify(task_context, proposed, confidence, contributions)
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
                classification_source = "inheritance"
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
            (
                proposed,
                confidence,
                classification_source,
                classifier_confidence,
                classifier_task_type,
                classifier_reason_hash,
            ) = self._maybe_classify(context, proposed, confidence, contributions)

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
        max_tier = Tier.parse(self.config.policy.max_tier)
        if proposed > max_tier:
            proposed = max_tier
            contributions.append(
                ScoreContribution(code=ReasonCode.QUOTA_LIMIT, weight=0, detail="policy max tier")
            )

        backend_name = backend_override or self.config.routing.backend_by_tier.get(
            str(proposed), "gpt"
        )
        backend = self.config.backends.get(backend_name)
        if backend_override:
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.BACKEND_OVERRIDE,
                    weight=0,
                    detail=f"explicit @{backend_override} backend override",
                )
            )
        if (
            backend is None
            or not backend.enabled
            or not backend_readiness(self.config, backend_name)[0]
        ):
            requested_backend = backend_name
            backend_name = "gpt"
            backend = self.config.backends[backend_name]
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.BACKEND_FALLBACK,
                    weight=0,
                    detail=f"backend {requested_backend} is unavailable; used gpt",
                )
            )
        if (
            proposed is Tier.MAX
            and context.current_tier is not Tier.MAX
            and not manual
            and backend_name == "gpt"
        ):
            proposed = Tier.SMART
            backend_name = backend_override or self.config.routing.backend_by_tier.get(
                str(proposed), "gpt"
            )
            backend = self.config.backends.get(backend_name)
            if (
                backend is None
                or not backend.enabled
                or not backend_readiness(self.config, backend_name)[0]
            ):
                backend_name = "gpt"
                backend = self.config.backends[backend_name]
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
        target = backend.target(proposed)
        digest = hashlib.sha256(context.latest_prompt.encode("utf-8")).hexdigest()
        comparison_tier = context.previous_task_tier or context.current_tier
        classifier_config = self.config.routing.classifier
        resolved_task_inherited = task_context_used
        classifier_attempted = classification_source in {
            "local_llm",
            "private_llm",
            "cloud_llm",
            "heuristic_fallback",
        }
        classifier_latency_ms = (
            getattr(self.classifier, "last_latency_ms", None) if classifier_attempted else None
        )
        classifier_request_hash = (
            getattr(self.classifier, "last_request_hash", None) if classifier_attempted else None
        )
        classifier_usage = (
            getattr(self.classifier, "last_usage", {}) if classifier_attempted else {}
        )
        previous_context_sent = bool(
            classifier_request_hash
            and classifier_config.include_previous_assistant
            and context.task_definition
        )
        task_context_used = resolved_task_inherited or previous_context_sent
        catalog_age = catalog_age_seconds(classifier_config)
        if catalog_age is None:
            catalog_status = "unverified"
        elif catalog_age <= classifier_config.catalog_ttl_seconds:
            catalog_status = "fresh"
        else:
            catalog_status = "stale"
        candidates: list[dict[str, object]] = []
        for candidate_tier in Tier:
            candidate_backend_name = self.config.routing.backend_by_tier.get(
                str(candidate_tier), "gpt"
            )
            candidate_backend = self.config.backends.get(candidate_backend_name)
            if (
                candidate_backend is None
                or not candidate_backend.enabled
                or not backend_readiness(self.config, candidate_backend_name)[0]
            ):
                candidate_backend_name = "gpt"
                candidate_backend = self.config.backends[candidate_backend_name]
            candidate_target = candidate_backend.target(candidate_tier)
            exclusions: list[str] = []
            if candidate_tier > max_tier:
                exclusions.append("policy_max_tier")
            if (
                candidate_tier is Tier.MAX
                and not manual
                and context.current_tier is not Tier.MAX
            ):
                exclusions.append("codex_step_safety_metadata_incompatible")
            candidates.append(
                {
                    "tier": str(candidate_tier),
                    "model": candidate_target.model,
                    "backend": candidate_backend_name,
                    "model_provider": candidate_backend.codex_provider,
                    "reasoning_effort": candidate_target.reasoning_effort,
                    "eligible": not exclusions,
                    "exclusions": exclusions,
                }
            )
        resolved_task = (
            context.task_definition
            if resolved_task_inherited and context.task_definition
            else context.latest_prompt
        )
        receipt: dict[str, object] = {
            "version": "selection-v1",
            "classifier": {
                "policy_version": CLASSIFIER_VERSION,
                "source": classification_source,
                "model": (
                    classifier_config.model
                    if classification_source in {"local_llm", "private_llm", "cloud_llm"}
                    else None
                ),
                "catalog_hash": classifier_config.catalog_hash,
                "catalog_checked_at": classifier_config.catalog_checked_at,
                "catalog_status": catalog_status,
                "provider_response": getattr(self.classifier, "last_response_metadata", {}),
                "request_hash": classifier_request_hash,
                "latency_ms": classifier_latency_ms,
                "usage": classifier_usage,
                "previous_context_sent": previous_context_sent,
                "previous_context_chars": getattr(
                    self.classifier, "last_previous_context_chars", 0
                ),
            },
            "resolved_task_hash": hashlib.sha256(resolved_task.encode("utf-8")).hexdigest(),
            "candidates": candidates,
            "proposed_tier": str(score_proposed),
            "selected": {
                "tier": str(proposed),
                "model": target.model,
                "backend": backend_name,
                "model_provider": backend.codex_provider,
                "reasoning_effort": target.reasoning_effort,
            },
            "policy": {
                "max_tier": str(max_tier),
                "risk_floor_applied": risk_floor_applied,
            },
            "context": {
                "previous_context_sent": previous_context_sent,
                "resolved_task_inherited": resolved_task_inherited,
            },
        }
        receipt_hash = hashlib.sha256(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return RouteDecision(
            tier=proposed,
            model=target.model,
            reasoning_effort=target.reasoning_effort,
            confidence=confidence,
            raw_score=round(raw_score, 2),
            reason_codes=reason_codes(contributions),
            contributions=contributions,
            provider=context.provider,
            backend=backend_name,
            model_provider=backend.codex_provider,
            route_scope=context.route_scope,
            agent_id=context.agent_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            prompt_hash=digest,
            manual_override=manual,
            inherited=inherited,
            switched=(
                proposed != comparison_tier
                or backend.codex_provider != context.current_model_provider
            ),
            proposed_tier=score_proposed,
            comparison_tier=comparison_tier,
            classifier_version=CLASSIFIER_VERSION,
            classification_source=classification_source,
            classifier_confidence=classifier_confidence,
            classifier_task_type=classifier_task_type,
            classifier_reason_hash=classifier_reason_hash,
            task_context_used=task_context_used,
            previous_context_sent=previous_context_sent,
            resolved_task_inherited=resolved_task_inherited,
            classifier_latency_ms=classifier_latency_ms,
            classifier_request_hash=classifier_request_hash,
            classifier_usage=classifier_usage,
            risk_floor_applied=risk_floor_applied,
            agent_requested_tier=context.agent_requested_tier,
            agent_request_reason_hash=context.agent_request_reason_hash,
            selection_receipt=receipt,
            selection_receipt_hash=receipt_hash,
        )

    def _maybe_classify(
        self,
        context: RouteContext,
        proposed: Tier,
        confidence: float,
        contributions: list[ScoreContribution],
    ) -> tuple[Tier, float, str, float | None, str | None, str | None]:
        routing = self.config.routing
        if routing.mode == "heuristic" or self.classifier is None:
            return proposed, confidence, "heuristic", None, None, None
        codes = {item.code for item in contributions}
        if ReasonCode.CREDENTIAL_EXPOSURE in codes or contains_credential(
            context.task_definition
        ):
            return proposed, confidence, "heuristic_sensitive", None, None, None
        if routing.mode == "hybrid" and confidence >= routing.classifier.ambiguity_threshold:
            return proposed, confidence, "heuristic", None, None, None
        try:
            result = self.classifier.classify(context)
        except Exception as error:
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.CLASSIFIER_FALLBACK,
                    weight=0,
                    detail=f"classifier unavailable ({type(error).__name__}); used heuristic",
                )
            )
            return proposed, confidence, "heuristic_fallback", None, None, None
        contributions.append(
            ScoreContribution(
                code=ReasonCode.LLM_CLASSIFIER,
                weight=0,
                detail=f"{self.classifier.source} selected {result.tier.name}",
            )
        )
        return (
            result.tier,
            result.confidence,
            self.classifier.source,
            result.confidence,
            result.task_type,
            result.reason_hash,
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
            ReasonCode.BOUNDED_COMMUNICATION,
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
