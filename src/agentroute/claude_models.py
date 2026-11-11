"""Claude model capabilities shared by routing and Responses translation."""

MODERN_CLAUDE_MODELS = frozenset(
    {
        "claude-haiku-5-5",
        "claude-sonnet-5-5",
        "claude-opus-5-5",
        "claude-fable-5-1",
        "claude-fable-5-2",
    }
)
EFFORT_MODELS = MODERN_CLAUDE_MODELS | {"claude-sonnet-5", "claude-opus-5"}
CLAUDE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
FORCED_TOOL_CHOICE_UNSUPPORTED = MODERN_CLAUDE_MODELS - {"claude-haiku-5-5"}
# Fable 5.2 validates every signed thinking block against the exact system
# prompt, tools and history that preceded it, for every account. Codex edits
# that prefix legitimately (compaction, routed instructions, tool catalog
# changes), so the bridge asks Anthropic to drop a mismatched block instead of
# failing the whole turn with a 400.
PREFIX_BOUND_THINKING_MODELS = frozenset({"claude-fable-5-2"})


def claude_effort(model: str, effort: str | None) -> str | None:
    """Normalize harness effort for models that support it, including Haiku 5.5."""
    if model not in EFFORT_MODELS or effort is None:
        return None
    normalized = {"none": "low", "minimal": "low", "ultra": "max", "persistent": "max"}.get(
        effort, effort
    )
    if normalized not in CLAUDE_EFFORT_LEVELS:
        raise ValueError(f"Unsupported Claude reasoning effort: {effort}")
    return normalized
