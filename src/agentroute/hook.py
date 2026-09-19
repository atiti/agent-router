from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any, TextIO

from .audit import AuditStore
from .capacity import backend_spend
from .config import AppConfig, load_config
from .models import ReasonCode, RouteContext, Tier
from .router import Router
from .signals import continues_previous_task, is_confirmation, route_overrides
from .transcript import parse_agent_model_request, previous_assistant_task, turn_token_usage


def _runtime_label() -> str | None:
    """Return a compact, non-sensitive label for the managed Codex runtime."""
    build_id = os.environ.get("AGENTROUTE_RUNTIME_BUILD_ID")
    if not build_id:
        return None
    marker = "-provider-routing-v"
    if marker in build_id:
        version = build_id.rsplit(marker, 1)[1]
        if version.isdigit():
            return f"v{version}"
    return "managed"


def _subagent_identity(payload: dict[str, Any]) -> tuple[str, str | None]:
    """Read Codex-native flat child fields, retaining nested compatibility."""
    nested = payload.get("subagent")
    subagent = nested if isinstance(nested, dict) else {}
    raw_agent_id = payload.get("agent_id") or subagent.get("agent_id")
    agent_id = str(raw_agent_id) if raw_agent_id else None
    return ("subagent" if agent_id else "root", agent_id)


def codex_user_prompt_submit(
    source: TextIO = sys.stdin,
    sink: TextIO = sys.stdout,
    *,
    config: AppConfig | None = None,
    store: AuditStore | None = None,
) -> int:
    """Handle Codex's UserPromptSubmit JSON protocol, failing open on errors."""
    try:
        payload: dict[str, Any] = json.load(source)
        config = config or load_config()
        provider = config.providers["codex"]
        current_model = str(payload.get("model", ""))
        current_tier = provider.tier_for_model(current_model)
        store = store or AuditStore()
        session_id = str(payload["session_id"])
        prompt = str(payload.get("prompt", ""))
        route_scope, agent_id = _subagent_identity(payload)
        previous_tier = store.previous_tier(session_id, route_scope, agent_id)
        tier_override, backend_override, routed_prompt = route_overrides(prompt)
        sticky_backend = store.route_preference(session_id, route_scope, agent_id)
        previous_capacity_status, previous_capacity_backend = store.previous_capacity(
            session_id, route_scope, agent_id
        )
        opaque_subagent_followup = (
            route_scope == "subagent" and not routed_prompt.strip() and previous_tier is not None
        )
        if opaque_subagent_followup and sticky_backend is None:
            sticky_backend = store.previous_backend(session_id, route_scope, agent_id)
        if tier_override == "auto":
            sticky_backend = None
        elif backend_override:
            sticky_backend = backend_override
        routing_input = "continue" if opaque_subagent_followup else prompt
        continuation = continues_previous_task(routing_input)
        classifier_needs_context = (
            config.routing.classifier.enabled
            and config.routing.classifier.include_previous_assistant
            and config.routing.mode in {"hybrid", "llm"}
        )
        task_definition = (
            previous_assistant_task(payload.get("transcript_path"))
            if continuation or classifier_needs_context
            else None
        )
        agent_request = (
            parse_agent_model_request(task_definition) if is_confirmation(routing_input) else None
        )
        daily_spend, monthly_spend = backend_spend(store.rows_since(), config)
        rate_limits = (
            dict(payload.get("rate_limits"))
            if isinstance(payload.get("rate_limits"), dict)
            else dict(payload.get("rateLimits"))
            if isinstance(payload.get("rateLimits"), dict)
            else {}
        )
        # `false` is authoritative: never use truthiness here, and preserve the
        # app-server's account-validated signal alongside sparse rolling windows.
        if payload.get("ordinary_usage_allowed") is not None:
            rate_limits["ordinary_usage_allowed"] = payload["ordinary_usage_allowed"]
        elif payload.get("ordinaryUsageAllowed") is not None:
            rate_limits["ordinary_usage_allowed"] = payload["ordinaryUsageAllowed"]
        context = RouteContext(
            session_id=session_id,
            turn_id=str(payload["turn_id"]) if payload.get("turn_id") else None,
            provider="codex",
            current_model_provider=str(payload.get("model_provider", "openai")),
            route_scope=route_scope,
            agent_id=agent_id,
            latest_prompt=routing_input,
            sticky_backend=sticky_backend,
            current_model=current_model,
            current_tier=current_tier,
            previous_task_tier=previous_tier,
            task_definition=task_definition,
            agent_requested_tier=(Tier.parse(agent_request.tier) if agent_request else None),
            agent_request_reason_hash=(
                hashlib.sha256(agent_request.reason.encode("utf-8")).hexdigest()
                if agent_request
                else None
            ),
            account_id=(
                str(payload.get("account_id")) if payload.get("account_id") else None
            ),
            rate_limits=rate_limits,
            backend_daily_spend=daily_spend,
            backend_monthly_spend=monthly_spend,
            previous_capacity_status=previous_capacity_status,
            previous_capacity_backend=previous_capacity_backend,
        )
        decision = Router(config).route(context)
        decision.strip_provider_state = store.provider_state_is_mixed(
            session_id, route_scope, agent_id
        ) or decision.model_provider != context.current_model_provider
        # @auto clears affinity even though the router's ordinary backend choice may be GPT.
        if tier_override == "auto":
            decision.sticky_backend = None
        store.record(
            decision,
            current_tier,
            context.latest_prompt if config.audit.store_prompts else None,
        )
        action = "Selected" if config.enabled else "Would select"
        reasons = ", ".join(code.value for code in decision.reason_codes) or "DEFAULT"
        confidence_kind = (
            "classifier confidence"
            if decision.classifier_confidence is not None
            else "rule confidence"
        )
        route_source = (
            f"LLM/{decision.classification_source.removesuffix('_llm')}"
            if decision.classification_source in {"local_llm", "private_llm", "cloud_llm"}
            else "DETERMINISTIC/FALLBACK"
            if decision.classification_source == "heuristic_fallback"
            else f"{decision.classification_source.upper()}"
        )
        output: dict[str, Any] = {
            "continue": not decision.capacity_blocked,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    "[AgentRoute routing metadata — not a user task]\n"
                    f"{action} {decision.tier.name} → {decision.model} "
                    f"({decision.confidence:.0%} {confidence_kind}); reasons: {reasons}.\n"
                    "If this tier is materially insufficient, end your final response with "
                    "two plain lines: MODEL_REQUEST: <FAST|NORMAL|SMART|MAX> and "
                    "MODEL_REQUEST_REASON: <one-line reason>. Request only when needed; it is "
                    "applied only after explicit user confirmation."
                ),
            },
        }
        if routed_prompt != prompt and routed_prompt:
            output["hookSpecificOutput"]["stripPromptPrefixBytes"] = len(
                prompt.encode("utf-8")
            ) - len(routed_prompt.encode("utf-8"))
        if config.enabled:
            specific = output["hookSpecificOutput"]
            specific["model"] = decision.model
            specific["modelProvider"] = decision.model_provider
            if decision.strip_provider_state:
                specific["stripProviderState"] = True
            if decision.reasoning_effort:
                specific["reasoningEffort"] = decision.reasoning_effort
            effort = (
                f" · {decision.reasoning_effort} reasoning"
                if decision.reasoning_effort
                else ""
            )
            specific["routeMessage"] = (
                f"◆ MODEL ROUTE · {decision.tier.name} → {decision.model}{effort}"
                f" · backend {decision.backend}/{decision.model_provider}"
                f" · scope {decision.route_scope}"
                f" · source {route_source}"
                f" · {confidence_kind} {decision.confidence:.0%}"
                f" · rule score {decision.raw_score:g}"
                + (
                    f" · {decision.classifier_task_type}"
                    if decision.classifier_task_type
                    else ""
                )
                + (
                    " · CREDENTIAL RISK"
                    if ReasonCode.CREDENTIAL_EXPOSURE in decision.reason_codes
                    else ""
                )
                + (
                    " · MAX→SMART SAFETY FALLBACK"
                    if ReasonCode.MODEL_COMPATIBILITY_FALLBACK in decision.reason_codes
                    else ""
                )
                + (
                    " · AGENT REQUEST APPROVED"
                    if ReasonCode.AGENT_ESCALATION in decision.reason_codes
                    else ""
                )
                + (
                    " · CLASSIFIER FALLBACK"
                    if ReasonCode.CLASSIFIER_FALLBACK in decision.reason_codes
                    else ""
                )
                + (
                    f" · CAPACITY {decision.capacity_status.upper()}: "
                    f"{decision.capacity_detail}"
                    if decision.capacity_status not in {"disabled", "healthy", "unknown"}
                    else f" · capacity {decision.capacity_detail}"
                    if decision.capacity_status == "healthy"
                    else ""
                )
                + (
                    f" · runtime {runtime_label}"
                    if (runtime_label := _runtime_label())
                    else ""
                )
            )
            if decision.capacity_blocked:
                output["stopReason"] = decision.capacity_detail
                output["systemMessage"] = (
                    "AgentRoute blocked this turn to avoid an unsafe or over-budget route. "
                    + (decision.capacity_detail or "No backend has available capacity.")
                )
        json.dump(output, sink, separators=(",", ":"))
        sink.write("\n")
        return 0
    except Exception as error:  # Hooks must not make Codex unusable.
        json.dump(
            {
                "continue": True,
                "systemMessage": f"AgentRoute failed open: {type(error).__name__}: {error}",
            },
            sink,
            separators=(",", ":"),
        )
        sink.write("\n")
        return 0


def codex_stop(
    source: TextIO = sys.stdin,
    sink: TextIO = sys.stdout,
    *,
    store: AuditStore | None = None,
) -> int:
    """Attach Codex's final per-turn token counters to the matching route decision."""
    try:
        payload: dict[str, Any] = json.load(source)
        route_scope, agent_id = _subagent_identity(payload)
        transcript_path = (
            payload.get("agent_transcript_path")
            if route_scope == "subagent"
            else payload.get("transcript_path")
        )
        usage = turn_token_usage(transcript_path, payload.get("turn_id"))
        reported_outcome = str(payload.get("turn_outcome") or payload.get("status") or "completed")
        outcome = (
            reported_outcome
            if reported_outcome in {"completed", "failed", "interrupted"}
            else "completed"
        )
        (store or AuditStore()).record_completion(
            str(payload["session_id"]),
            str(payload["turn_id"]),
            str(payload.get("model", "")),
            usage,
            outcome=outcome,
            completion_source="stop_hook",
            route_scope=route_scope,
            agent_id=agent_id,
        )
        json.dump({"continue": True, "suppressOutput": True}, sink, separators=(",", ":"))
        sink.write("\n")
        return 0
    except Exception:  # Usage accounting must never prevent a turn from stopping.
        json.dump({"continue": True, "suppressOutput": True}, sink, separators=(",", ":"))
        sink.write("\n")
        return 0
