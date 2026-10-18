"""Bound the display preview independently of Claude's signed replay data."""

from typing import Any

MAX_SUMMARY_BYTES = 512


def summary_delta(block: dict[str, Any], text: str) -> str:
    """Append at most 512 UTF-8 bytes to a reasoning item's visible summary."""
    previous = block.get("summary_text", "")
    remaining = MAX_SUMMARY_BYTES - len(previous.encode())
    delta = text.encode()[:remaining].decode(errors="ignore")
    block["summary_text"] = previous + delta
    return delta
