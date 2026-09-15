from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MAX_TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024
MAX_TASK_DEFINITION_CHARS = 12_000


def previous_assistant_task(transcript_path: object) -> str | None:
    """Return the latest assistant final answer from a bounded local transcript tail."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    path = Path(transcript_path).expanduser()
    try:
        with path.open("rb") as source:
            source.seek(0, 2)
            size = source.tell()
            offset = max(0, size - MAX_TRANSCRIPT_TAIL_BYTES)
            source.seek(offset)
            data = source.read()
    except OSError:
        return None

    lines = data.splitlines()
    if offset and lines:
        lines = lines[1:]
    for raw_line in reversed(lines):
        try:
            event: dict[str, Any] = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if event.get("type") != "response_item":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != "message" or payload.get("role") != "assistant":
            continue
        if payload.get("phase") not in (None, "final_answer"):
            continue
        content = payload.get("content")
        if not isinstance(content, list):
            continue
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "output_text"
        ]
        text = "\n".join(part for part in parts if isinstance(part, str)).strip()
        if text:
            return text[:MAX_TASK_DEFINITION_CHARS]
    return None
