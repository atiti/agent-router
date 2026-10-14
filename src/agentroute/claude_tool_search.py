"""Keep large Claude tool catalogs available without loading every schema into context."""

import json
from typing import Any

from .claude_models import MODERN_CLAUDE_MODELS
from .claude_thinking_store import ThinkingReplayError

SEARCH_TOOL_NAME = "tool_search_tool_regex"
SEARCH_BLOCK_TYPES = frozenset({"server_tool_use", "tool_search_tool_result"})
MAX_SEARCH_BLOCK_BYTES = 6_000
DIRECT_TOOLS = frozenset(
    {
        "exec_command",
        "write_stdin",
        "apply_patch",
        "view_image",
        "create_goal",
        "get_goal",
        "update_goal",
        "request_user_input",
        "request_user_input_async",
    }
)


def validate_search_block(block: dict[str, Any]) -> None:
    """Bound rehydrated provider context as well as its compact Codex reference.

    Preserve native blocks exactly or fail explicitly: truncating search results
    would corrupt the provider transcript. Normal searches return five references.
    """
    if (
        block.get("type") in SEARCH_BLOCK_TYPES
        and len(json.dumps(block, separators=(",", ":")).encode()) > MAX_SEARCH_BLOCK_BYTES
    ):
        raise ThinkingReplayError(
            "Claude tool search exceeded the 6 KB replay limit; retry with a narrower search."
        )


def defer_large_catalog(payload: dict[str, Any]) -> None:
    """Use native search for supported models; preserve every tool schema verbatim.

    Small catalogs and explicit forced-tool requests keep their existing behavior.
    Search executes on Anthropic, so it never becomes a local Codex tool call.
    """
    tools = payload.get("tools") or []
    if (
        payload.get("model") not in MODERN_CLAUDE_MODELS
        or payload.get("tool_choice", {}).get("type") not in {"auto", "none"}
        or any(tool.get("name") == SEARCH_TOOL_NAME for tool in tools)
        or (len(tools) < 32 and len(json.dumps(tools).encode()) < 64_000)
    ):
        return
    deferred = [tool for tool in tools if tool.get("name") not in DIRECT_TOOLS]
    if not deferred:
        return
    for tool in deferred:
        tool["defer_loading"] = True
    tools.append({"type": "tool_search_tool_regex_20251119", "name": SEARCH_TOOL_NAME})
