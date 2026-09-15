from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from .audit import AuditStore
from .config import AppConfig, load_config
from .models import RouteContext
from .router import Router


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
        context = RouteContext(
            session_id=session_id,
            provider="codex",
            latest_prompt=str(payload.get("prompt", "")),
            current_model=current_model,
            current_tier=current_tier,
            previous_task_tier=previous_tier,
        )
        decision = Router(config).route(context)
        store.record(
            decision,
            current_tier,
            context.latest_prompt if config.audit.store_prompts else None,
        )
        action = "Selected" if config.enabled else "Would select"
        reasons = ", ".join(code.value for code in decision.reason_codes) or "DEFAULT"
        output: dict[str, Any] = {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    "[AgentRoute routing metadata — not a user task]\n"
                    f"{action} {decision.tier.name} → {decision.model} "
                    f"({decision.confidence:.0%}); reasons: {reasons}."
                ),
            },
        }
        if config.enabled:
            specific = output["hookSpecificOutput"]
            specific["model"] = decision.model
            if decision.reasoning_effort:
                specific["reasoningEffort"] = decision.reasoning_effort
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
