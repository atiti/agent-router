from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any, TextIO

from .audit import AuditStore
from .config import AppConfig, load_config
from .models import ReasonCode, RouteContext, Tier
from .router import Router
from .signals import continues_previous_task, is_confirmation
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
        previous_tier = store.previous_tier(session_id)
        prompt = str(payload.get("prompt", ""))
        subagent = payload.get("subagent") or {}
        route_scope = "subagent" if subagent else "root"
        continuation = continues_previous_task(prompt)
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
            parse_agent_model_request(task_definition) if is_confirmation(prompt) else None
        )
        context = RouteContext(
            session_id=session_id,
            turn_id=str(payload["turn_id"]) if payload.get("turn_id") else None,
            provider="codex",
            current_model_provider=str(payload.get("model_provider", "openai")),
            route_scope=route_scope,
            agent_id=(str(subagent.get("agent_id")) if subagent.get("agent_id") else None),
            latest_prompt=prompt,
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
        )
        decision = Router(config).route(context)
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
            "continue": True,
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
        if config.enabled:
            specific = output["hookSpecificOutput"]
            specific["model"] = decision.model
            specific["modelProvider"] = decision.model_provider
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
                    f" · runtime {runtime_label}"
                    if (runtime_label := _runtime_label())
                    else ""
                )
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
        usage = turn_token_usage(payload.get("transcript_path"), payload.get("turn_id"))
        if usage:
            (store or AuditStore()).record_usage(
                str(payload["session_id"]),
                str(payload["turn_id"]),
                str(payload.get("model", "")),
                usage,
            )
        json.dump({"continue": True, "suppressOutput": True}, sink, separators=(",", ":"))
        sink.write("\n")
        return 0
    except Exception:  # Usage accounting must never prevent a turn from stopping.
        json.dump({"continue": True, "suppressOutput": True}, sink, separators=(",", ":"))
        sink.write("\n")
        return 0
