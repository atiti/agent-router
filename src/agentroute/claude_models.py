"""Claude model capabilities shared by routing and Responses translation."""

MODERN_CLAUDE_MODELS = frozenset(
    {
        "claude-haiku-5-5",
        "claude-sonnet-5-5",
        "claude-opus-5-5",
        "claude-fable-5-1",
    }
)
EFFORT_MODELS = MODERN_CLAUDE_MODELS | {"claude-sonnet-5", "claude-opus-5"}
CLAUDE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
FORCED_TOOL_CHOICE_UNSUPPORTED = MODERN_CLAUDE_MODELS - {"claude-haiku-5-5"}


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
