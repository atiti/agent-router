from __future__ import annotations

import hashlib
import json
import math

from .capacity import backend_state, fallback_chain
from .classifier import (
    JevShadowClassifier,
    OpenAICompatibleClassifier,
    TierClassifier,
    catalog_age_seconds,
)
from .config import AppConfig, load_config, model_capabilities
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

CLASSIFIER_VERSION = "hybrid-v9"

FAST_SAFE_TASK_TYPES = {
    "greeting",
    "acknowledgment",
    "no_op",
    "status",
    "retrieval",
    "formatting",
}
REASONING_EFFORT_RANK = {
    "none": 0,
    "minimal": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "xhigh": 5,
    "ultra": 6,
    "persistent": 7,
}


def _stronger_reasoning_effort(*values: str | None) -> str | None:
    available = [value for value in values if value]
    return max(available, key=lambda value: REASONING_EFFORT_RANK.get(value, -1), default=None)


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
        fallback_classifier: TierClassifier | None = None,
    ) -> None:
        self.config = config or load_config()
        classifier_config = self.config.routing.classifier
        self.classifier = classifier
        if self.classifier is None and classifier_config.enabled:
            self.classifier = (
                JevShadowClassifier(classifier_config.jev_shadow)
                if classifier_config.engine == "jev"
                else OpenAICompatibleClassifier(classifier_config)
            )
        self.fallback_classifier = fallback_classifier
        if (
            self.fallback_classifier is None
            and classifier_config.enabled
            and classifier_config.engine == "jev"
            and classifier_config.jev_shadow.llm_fallback_enabled
        ):
            self.fallback_classifier = OpenAICompatibleClassifier(classifier_config)
        self._last_classifier: TierClassifier | None = None
        self._jev_observation: dict[str, object] | None = None

    def route(self, context: RouteContext) -> RouteDecision:
        self._last_classifier = None
        self._jev_observation = None
        override, backend_override, parsed_reasoning_effort, _ = route_overrides(
            context.latest_prompt, self.config.backends
        )
        reasoning_effort_override = (
            context.requested_reasoning_effort or parsed_reasoning_effort
        )
        contributions = extract_signals(context)
        if reasoning_effort_override:
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.REASONING_EFFORT_OVERRIDE,
                    weight=0,
                    detail=f"explicit @{reasoning_effort_override} reasoning override",
                )
            )
        raw_score = sum(item.weight for item in contributions)
        inherited = False
        task_context_used = False
        manual = override not in (None, "auto")
        classification_source = "heuristic"
        classifier_confidence: float | None = None
        classifier_task_type: str | None = None
        classifier_reasoning_effort: str | None = None
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
            if context.sticky_backend and context.previous_task_tier is not None:
                proposed = context.previous_task_tier
                confidence = 0.99
                classification_source = "session_affinity"
                contributions.append(
                    ScoreContribution(
                        code=ReasonCode.SESSION_AFFINITY,
                        weight=0,
                        detail="follow-up retained the explicit session route model",
                    )
                )
            elif context.task_definition:
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
                    classifier_reasoning_effort,
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
                classifier_reasoning_effort,
            ) = self._maybe_classify(context, proposed, confidence, contributions)

        if (
            not manual
            and self.config.routing.fast_quality_floor
            and classification_source
            in {
                "local_llm",
                "private_llm",
                "cloud_llm",
                "local_llm_fallback",
                "private_llm_fallback",
                "cloud_llm_fallback",
                "local_jev",
            }
            and proposed is Tier.FAST
            and classifier_task_type not in FAST_SAFE_TASK_TYPES
        ):
            proposed = Tier.NORMAL
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.FAST_QUALITY_FLOOR,
                    weight=0,
                    detail=f"{classifier_task_type or 'unknown'} requires at least NORMAL",
                )
            )
        if (
            not manual
            and inherited
            and self.config.routing.continuation_capability_floor
            and context.previous_task_tier is not None
            and proposed < context.previous_task_tier
        ):
            proposed = context.previous_task_tier
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.CONTINUATION_CAPABILITY_FLOOR,
                    weight=0,
                    detail="continuation retained the previous task's model tier",
                )
            )

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

        backend_name = (
            backend_override
            or context.sticky_backend
            or context.requested_backend
            or context.inherited_backend
            or self.config.routing.backend_by_tier.get(str(proposed), "gpt")
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
        elif context.sticky_backend:
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.SESSION_AFFINITY,
                    weight=0,
                    detail=f"continued explicit {context.sticky_backend} session route",
                )
            )
        elif context.requested_backend:
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.SPAWN_BACKEND_OVERRIDE,
                    weight=0,
                    detail=f"explicit child {context.requested_backend} backend override",
                )
            )
        elif context.inherited_backend:
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.PROVIDER_INHERITANCE,
                    weight=0,
                    detail=(
                        f"subagent retained inherited {context.inherited_backend} provider"
                    ),
                )
            )
        backend_unavailable = bool(
            backend is None
            or not backend.enabled
            or not backend_readiness(self.config, backend_name)[0]
        )
        explicit_child_backend_unavailable = bool(
            context.requested_backend and backend_unavailable
        )
        explicit_child_model_mismatch = bool(
            context.requested_backend
            and context.spawn_model_explicit
            and backend is not None
            and context.current_model
            and context.current_model
            not in {target.model for target in backend.tiers.values()}
        )
        unavailable_requested_backend: str | None = None
        unavailable_locked = False
        if backend_unavailable:
            unavailable_requested_backend = backend_name
            unavailable_locked = bool(
                backend_override or context.sticky_backend or context.requested_backend
            )
            selected_ready: str | None = None
            if not unavailable_locked or (
                not self.config.capacity.enabled and not context.requested_backend
            ):
                candidates = (
                    fallback_chain(self.config, backend_name)
                    if self.config.capacity.enabled
                    else ["gpt"]
                )
                for candidate in candidates:
                    configured = self.config.backends.get(candidate)
                    if (
                        configured is not None
                        and configured.enabled
                        and backend_readiness(self.config, candidate)[0]
                    ):
                        selected_ready = candidate
                        break
            backend_name = selected_ready or (
                unavailable_requested_backend
                if unavailable_requested_backend in self.config.backends
                else "gpt"
            )
            backend = self.config.backends[backend_name]
            contributions.append(
                ScoreContribution(
                    code=(
                        ReasonCode.CAPACITY_BLOCKED
                        if unavailable_locked and self.config.capacity.enabled
                        else ReasonCode.BACKEND_FALLBACK
                    ),
                    weight=0,
                    detail=(
                        f"explicit session route {unavailable_requested_backend} is unavailable"
                        if unavailable_locked and self.config.capacity.enabled
                        else f"backend {unavailable_requested_backend} is unavailable; "
                        f"used {backend_name}"
                    ),
                )
            )
        if (
            proposed is Tier.MAX
            and context.current_tier is not Tier.MAX
            and not manual
            and backend_name == "gpt"
        ):
            proposed = Tier.SMART
            backend_name = (
                backend_override
                or context.sticky_backend
                or context.requested_backend
                or context.inherited_backend
                or self.config.routing.backend_by_tier.get(str(proposed), "gpt")
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

        capacity_requested_backend = backend_name
        capacity = backend_state(
            self.config,
            backend_name,
            rate_limits=context.rate_limits,
            account_id=context.account_id,
            daily_spend=context.backend_daily_spend.get(backend_name, 0.0),
            monthly_spend=context.backend_monthly_spend.get(backend_name, 0.0),
            recovery_hold=(
                context.previous_capacity_backend == backend_name
                and context.previous_capacity_status in {"fallback", "blocked", "exhausted"}
            ),
        )
        capacity_blocked = False
        capacity_detail = capacity.detail
        capacity_trigger = capacity.trigger
        capacity_status = capacity.status
        capacity_locked = bool(
            backend_override or context.sticky_backend or context.requested_backend
        )
        if explicit_child_model_mismatch:
            capacity_blocked = True
            capacity_status = "blocked"
            capacity_trigger = "spawn_model_backend_mismatch"
            capacity_requested_backend = context.requested_backend
            capacity_detail = (
                f"explicit child model {context.current_model} is not configured for "
                f"backend {context.requested_backend}"
            )
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.CAPACITY_BLOCKED,
                    weight=0,
                    detail=capacity_detail,
                )
            )
        elif explicit_child_backend_unavailable:
            capacity_blocked = True
            capacity_status = "blocked"
            capacity_trigger = "spawn_backend_unavailable"
            capacity_requested_backend = unavailable_requested_backend
            capacity_detail = (
                f"explicit child backend {unavailable_requested_backend} is unavailable. "
                "Choose a configured, ready backend or omit backend to inherit the parent."
            )
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.CAPACITY_BLOCKED,
                    weight=0,
                    detail=capacity_detail,
                )
            )
        elif unavailable_locked and self.config.capacity.enabled:
            capacity_blocked = True
            capacity_status = "blocked"
            capacity_trigger = "backend_unavailable"
            capacity_requested_backend = unavailable_requested_backend
            capacity_detail = (
                f"explicit session route {unavailable_requested_backend} is unavailable. "
                "Use @auto to permit backend fallback."
            )
        elif capacity.status == "warning":
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.CAPACITY_WARNING,
                    weight=0,
                    detail=capacity.detail,
                )
            )
        elif not capacity.available:
            if capacity_locked:
                capacity_blocked = True
                capacity_status = "blocked"
                capacity_detail = (
                    f"{capacity.detail}; explicit session route {backend_name} is "
                    "capacity-locked. Configure another signed-in ChatGPT profile or use "
                    "@auto to permit backend fallback."
                )
                contributions.append(
                    ScoreContribution(
                        code=ReasonCode.CAPACITY_BLOCKED,
                        weight=0,
                        detail=capacity_detail,
                    )
                )
            else:
                fallback_details: list[str] = []
                selected_fallback: str | None = None
                for candidate in fallback_chain(self.config, backend_name):
                    candidate_backend = self.config.backends.get(candidate)
                    ready = bool(
                        candidate_backend
                        and candidate_backend.enabled
                        and backend_readiness(self.config, candidate)[0]
                    )
                    if not ready:
                        fallback_details.append(f"{candidate}: unavailable")
                        continue
                    candidate_capacity = backend_state(
                        self.config,
                        candidate,
                        rate_limits=context.rate_limits,
                        account_id=context.account_id,
                        daily_spend=context.backend_daily_spend.get(candidate, 0.0),
                        monthly_spend=context.backend_monthly_spend.get(candidate, 0.0),
                    )
                    if not candidate_capacity.available:
                        fallback_details.append(f"{candidate}: {candidate_capacity.detail}")
                        continue
                    selected_fallback = candidate
                    break
                if selected_fallback:
                    previous_backend = backend_name
                    backend_name = selected_fallback
                    backend = self.config.backends[backend_name]
                    capacity_status = "fallback"
                    capacity_detail = (
                        f"{previous_backend} capacity unavailable ({capacity.detail}); "
                        f"using {backend_name}"
                    )
                    contributions.append(
                        ScoreContribution(
                            code=ReasonCode.CAPACITY_FALLBACK,
                            weight=0,
                            detail=capacity_detail,
                        )
                    )
                else:
                    capacity_blocked = True
                    capacity_status = "blocked"
                    attempted = "; ".join(fallback_details) or "no fallback configured"
                    capacity_detail = (
                        f"{backend_name} capacity unavailable ({capacity.detail}); "
                        f"{attempted}. No safe backend has capacity."
                    )
                    contributions.append(
                        ScoreContribution(
                            code=ReasonCode.CAPACITY_BLOCKED,
                            weight=0,
                            detail=capacity_detail,
                        )
                    )
        target = backend.target(proposed)
        inherited_effort = context.previous_reasoning_effort if inherited else None
        selected_reasoning_effort = reasoning_effort_override or (
            _stronger_reasoning_effort(classifier_reasoning_effort, inherited_effort)
            if classifier_reasoning_effort or inherited_effort
            else target.reasoning_effort
        )
        reasoning_effort_source = (
            "manual"
            if reasoning_effort_override
            else "classifier+inheritance"
            if classifier_reasoning_effort and inherited_effort
            else "classifier"
            if classifier_reasoning_effort
            else "inheritance"
            if inherited and context.previous_reasoning_effort
            else "tier_default"
        )
        digest = hashlib.sha256(context.latest_prompt.encode("utf-8")).hexdigest()
        comparison_tier = context.previous_task_tier or context.current_tier
        classifier_config = self.config.routing.classifier
        resolved_task_inherited = task_context_used
        classifier_attempted = classification_source in {
            "local_llm",
            "private_llm",
            "cloud_llm",
            "local_llm_fallback",
            "private_llm_fallback",
            "cloud_llm_fallback",
            "local_jev",
            "heuristic_jev_low_confidence",
            "heuristic_fallback",
        }
        classifier_latency_ms = (
            getattr(self._last_classifier, "last_latency_ms", None)
            if classifier_attempted
            else None
        )
        classifier_request_hash = (
            getattr(self._last_classifier, "last_request_hash", None)
            if classifier_attempted
            else None
        )
        classifier_usage = (
            getattr(self._last_classifier, "last_usage", {}) if classifier_attempted else {}
        )
        default_classifier_status = (
            "succeeded"
            if classification_source
            in {
                "local_llm",
                "private_llm",
                "cloud_llm",
                "local_llm_fallback",
                "private_llm_fallback",
                "cloud_llm_fallback",
                "local_jev",
                "heuristic_jev_low_confidence",
            }
            else "error"
        )
        classifier_status = (
            getattr(self._last_classifier, "last_status", default_classifier_status)
            if classifier_attempted
            else "skipped"
        )
        if classifier_status == "started":
            classifier_status = "error"
        classifier_error_type = (
            getattr(self._last_classifier, "last_error_type", None)
            if classifier_attempted
            else None
        )
        previous_context_sent = bool(
            classifier_request_hash
            and getattr(self._last_classifier, "last_previous_context_chars", 0)
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
            candidate_capabilities = model_capabilities(
                self.config, candidate_backend_name, candidate_target.model
            )
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
                    "capabilities": candidate_capabilities.model_dump(
                        mode="json", exclude_none=True
                    ),
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
                    getattr(self._last_classifier, "config", classifier_config).model
                    if classifier_attempted
                    else None
                ),
                "acceptance_threshold": (
                    classifier_config.jev_shadow.acceptance_threshold
                    if classifier_config.engine == "jev"
                    else None
                ),
                "status": classifier_status,
                "error_type": classifier_error_type,
                "catalog_hash": classifier_config.catalog_hash,
                "catalog_checked_at": classifier_config.catalog_checked_at,
                "catalog_status": catalog_status,
                "provider_response": getattr(
                    self._last_classifier, "last_response_metadata", {}
                ),
                "request_hash": classifier_request_hash,
                "latency_ms": classifier_latency_ms,
                "usage": classifier_usage,
                "previous_context_sent": previous_context_sent,
                "previous_context_chars": getattr(
                    self._last_classifier, "last_previous_context_chars", 0
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
                "reasoning_effort": selected_reasoning_effort,
            },
            "capacity": {
                "status": capacity_status,
                "detail": capacity_detail,
                "trigger": capacity_trigger,
                "requested_backend": capacity_requested_backend,
                "selected_backend": backend_name,
                "blocked": capacity_blocked,
            },
            "policy": {
                "max_tier": str(max_tier),
                "risk_floor_applied": risk_floor_applied,
                "reasoning_effort_override": reasoning_effort_override,
                "classifier_reasoning_effort": classifier_reasoning_effort,
                "reasoning_effort_source": reasoning_effort_source,
            },
            "context": {
                "previous_context_sent": previous_context_sent,
                "resolved_task_inherited": resolved_task_inherited,
            },
        }
        if self._jev_observation is not None:
            receipt["jev_first_pass"] = self._jev_observation
        receipt_hash = hashlib.sha256(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return RouteDecision(
            tier=proposed,
            model=target.model,
            reasoning_effort=selected_reasoning_effort,
            requested_reasoning_effort=reasoning_effort_override,
            confidence=confidence,
            raw_score=round(raw_score, 2),
            reason_codes=reason_codes(contributions),
            contributions=contributions,
            provider=context.provider,
            backend=backend_name,
            model_provider=backend.codex_provider,
            sticky_backend=(
                backend_override or context.sticky_backend or context.requested_backend
            ),
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
            classifier_reasoning_effort=classifier_reasoning_effort,
            reasoning_effort_source=reasoning_effort_source,
            classifier_reason_hash=classifier_reason_hash,
            task_context_used=task_context_used,
            previous_context_sent=previous_context_sent,
            resolved_task_inherited=resolved_task_inherited,
            classifier_latency_ms=classifier_latency_ms,
            classifier_request_hash=classifier_request_hash,
            classifier_usage=classifier_usage,
            classifier_status=classifier_status,
            classifier_error_type=classifier_error_type,
            risk_floor_applied=risk_floor_applied,
            agent_requested_tier=context.agent_requested_tier,
            agent_request_reason_hash=context.agent_request_reason_hash,
            selection_receipt=receipt,
            selection_receipt_hash=receipt_hash,
            capacity_status=capacity_status,
            capacity_detail=capacity_detail,
            capacity_trigger=capacity_trigger,
            capacity_requested_backend=capacity_requested_backend,
            capacity_account_id=(
                hashlib.sha256(context.account_id.encode("utf-8")).hexdigest()[:16]
                if context.account_id
                else None
            ),
            capacity_blocked=capacity_blocked,
        )

    def _maybe_classify(
        self,
        context: RouteContext,
        proposed: Tier,
        confidence: float,
        contributions: list[ScoreContribution],
    ) -> tuple[Tier, float, str, float | None, str | None, str | None, str | None]:
        routing = self.config.routing
        if routing.mode == "heuristic" or self.classifier is None:
            return proposed, confidence, "heuristic", None, None, None, None
        codes = {item.code for item in contributions}
        if ReasonCode.CREDENTIAL_EXPOSURE in codes or contains_credential(
            context.task_definition
        ):
            return proposed, confidence, "heuristic_sensitive", None, None, None, None
        if (
            routing.mode == "hybrid"
            and routing.classifier.engine != "jev"
            and confidence >= routing.classifier.ambiguity_threshold
        ):
            return proposed, confidence, "heuristic", None, None, None, None
        if (
            routing.classifier.engine == "jev"
            and proposed is Tier.FAST
            and confidence
            >= routing.classifier.jev_shadow.deterministic_fast_bypass_confidence
        ):
            return proposed, confidence, "heuristic", None, None, None, None
        try:
            result = self.classifier.classify(context)
            self._last_classifier = self.classifier
        except Exception as error:
            if getattr(self.classifier, "last_status", None) in {None, "started"}:
                self.classifier.last_status = "error"
            if getattr(self.classifier, "last_error_type", None) is None:
                self.classifier.last_error_type = type(error).__name__
            self._last_classifier = self.classifier
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.CLASSIFIER_FALLBACK,
                    weight=0,
                    detail=f"classifier unavailable ({type(error).__name__}); used heuristic",
                )
            )
            return proposed, confidence, "heuristic_fallback", None, None, None, None
        if self.classifier.source == "local_jev":
            self._jev_observation = {
                "tier": str(result.tier),
                "confidence": result.confidence,
                "task_type": result.task_type.value,
                "acceptance_threshold": (
                    self.config.routing.classifier.jev_shadow.acceptance_threshold
                ),
                "tier_signals": getattr(self.classifier, "last_tier_signals", {}),
            }
        if (
            self.classifier.source == "local_jev"
            and result.confidence
            < self.config.routing.classifier.jev_shadow.acceptance_threshold
        ):
            contributions.append(
                ScoreContribution(
                    code=ReasonCode.JEV_LOW_CONFIDENCE,
                    weight=0,
                    detail=(
                        f"local JEV confidence {result.confidence:.0%} below "
                        f"{self.config.routing.classifier.jev_shadow.acceptance_threshold:.0%}; "
                        "requesting configured local LLM fallback"
                        if self.fallback_classifier is not None
                        else "retained heuristic route"
                    ),
                )
            )
            if self.fallback_classifier is not None:
                try:
                    fallback_result = self.fallback_classifier.classify(context)
                    self._last_classifier = self.fallback_classifier
                except Exception as error:
                    if getattr(self.fallback_classifier, "last_status", None) in {None, "started"}:
                        self.fallback_classifier.last_status = "error"
                    if getattr(self.fallback_classifier, "last_error_type", None) is None:
                        self.fallback_classifier.last_error_type = type(error).__name__
                    self._last_classifier = self.fallback_classifier
                    contributions.append(
                        ScoreContribution(
                            code=ReasonCode.CLASSIFIER_FALLBACK,
                            weight=0,
                            detail=(
                                "local JEV was low confidence and LLM fallback was unavailable "
                                f"({type(error).__name__}); used heuristic"
                            ),
                        )
                    )
                    return proposed, confidence, "heuristic_fallback", None, None, None, None
                contributions.append(
                    ScoreContribution(
                        code=ReasonCode.LLM_CLASSIFIER,
                        weight=0,
                        detail=(
                            f"{self.fallback_classifier.source} selected "
                            f"{fallback_result.tier.name} after low-confidence local JEV"
                        ),
                    )
                )
                return (
                    fallback_result.tier,
                    fallback_result.confidence,
                    f"{self.fallback_classifier.source}_fallback",
                    fallback_result.confidence,
                    fallback_result.task_type.value,
                    fallback_result.reason_hash,
                    fallback_result.reasoning_effort,
                )
            return (
                proposed,
                confidence,
                "heuristic_jev_low_confidence",
                result.confidence,
                result.task_type.value,
                result.reason_hash,
                None,
            )
        contributions.append(
            ScoreContribution(
                code=(
                    ReasonCode.JEV_CLASSIFIER
                    if self.classifier.source == "local_jev"
                    else ReasonCode.LLM_CLASSIFIER
                ),
                weight=0,
                detail=f"{self.classifier.source} selected {result.tier.name}",
            )
        )
        return (
            result.tier,
            result.confidence,
            self.classifier.source,
            result.confidence,
            result.task_type.value,
            result.reason_hash,
            result.reasoning_effort,
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
