"""Apply provider cache boundaries without changing conversation content or order."""

import json
from typing import Any

from .config import ClaudeCacheConfig


def apply_prompt_cache(payload: dict[str, Any], settings: ClaudeCacheConfig) -> None:
    if settings.mode == "off":
        return
    marker = {"type": "ephemeral", "ttl": settings.ttl}
    # Automatic caching follows the growing conversation. Explicit breakpoints
    # protect the stable tools/system prefixes when later context changes.
    payload["cache_control"] = dict(marker)
    tools = payload.get("tools") or []
    if tools:
        tools[-1]["cache_control"] = dict(marker)
    system = payload.get("system") or []
    if system:
        system[-1]["cache_control"] = dict(marker)


def cache_boundaries(payload: dict[str, Any]) -> dict[str, int]:
    """Measured serialized prefix bytes; never retain text, hashes, or token estimates."""
    return {
        f"{section}_bytes": len(json.dumps(payload.get(section, []), ensure_ascii=False).encode())
        if payload.get(section)
        else 0
        for section in ("tools", "system", "messages")
    }
