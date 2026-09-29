"""Claude model capabilities shared by routing and Responses translation."""

MODERN_CLAUDE_MODELS = frozenset(
    {
        "claude-sonnet-5-5",
        "claude-opus-5-5",
        "claude-fable-5-1",
    }
)
EFFORT_MODELS = MODERN_CLAUDE_MODELS | {"claude-sonnet-5", "claude-opus-5"}
CLAUDE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def claude_effort(model: str, effort: str | None) -> str | None:
    """Normalize harness effort; Haiku has no effort parameter."""
    if model not in EFFORT_MODELS or effort is None:
        return None
    normalized = {"none": "low", "minimal": "low", "ultra": "max", "persistent": "max"}.get(
        effort, effort
    )
    if normalized not in CLAUDE_EFFORT_LEVELS:
        raise ValueError(f"Unsupported Claude reasoning effort: {effort}")
    return normalized
