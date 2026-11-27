"""Read checkpoint ownership without changing or exporting saved conversation content."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MAX_SCAN_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class Checkpoint:
    provider: str | None
    created_at: str


def latest_checkpoint(path: object, session_id: str) -> Checkpoint | None:
    """Inspect the newest replacement history, bounded to 64 MiB of complete lines.

    A child must supply its own transcript; parent checkpoint ownership is not
    evidence for a different thread. Missing evidence never clears native guards.
    """
    if not isinstance(path, str) or not path:
        return Checkpoint(None, "")
    corrupt_tail = False
    try:
        with Path(path).open("rb") as source:
            first = json.loads(source.readline())
            if first.get("type") != "session_meta" or session_id not in {
                first.get("payload", {}).get("id"),
                first.get("payload", {}).get("session_id"),
            }:
                return Checkpoint(None, "")
            source.seek(0, 2)
            end = source.tell()
            offset = max(0, end - MAX_SCAN_BYTES)
            source.seek(offset)
            data = source.read()
        lines = data.splitlines()
        if offset:
            lines = lines[1:]
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                corrupt_tail = True
                continue
            payload = event.get("payload", {})
            if event.get("type") != "compacted":
                continue
            history = payload.get("replacement_history")
            if corrupt_tail or not isinstance(history, list):
                return Checkpoint(None, "")
            allowed = {
                "message",
                "reasoning",
                "function_call",
                "function_call_output",
                "custom_tool_call",
                "custom_tool_call_output",
                "local_shell_call",
                "web_search_call",
                "image_generation_call",
                "tool_search_call",
                "tool_search_output",
                "agent_message",
                "additional_tools",
                "configuration_update",
                "compaction",
                "context_compaction",
            }
            if any(
                not isinstance(item, dict) or item.get("type") not in allowed for item in history
            ):
                return Checkpoint(None, "")
            if not history:
                return Checkpoint(None, "")
            for item in history:
                kind = item["type"]
                if kind == "message" and (
                    item.get("role") not in {"system", "developer", "user", "assistant"}
                    or not isinstance(item.get("content"), list)
                ):
                    return Checkpoint(None, "")
                if kind == "reasoning" and not isinstance(item.get("summary"), list):
                    return Checkpoint(None, "")
            indices = [
                i
                for i, item in enumerate(history)
                if item.get("type") in {"compaction", "context_compaction"}
            ]
            if not indices:
                if any(
                    item.get("encrypted_content") or item.get("encrypted_function_args")
                    for item in history
                ):
                    return Checkpoint(None, "")
                return None
            metadata = payload.get("replacement_history_metadata") or []
            owners = {
                metadata[i].get("model_provider_id")
                if i < len(metadata) and isinstance(metadata[i], dict)
                else None
                for i in indices
            }
            timestamp = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
            return Checkpoint(
                next(iter(owners)) if len(owners) == 1 else None,
                timestamp.isoformat(),
            )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return Checkpoint(None, "")
    # A bounded tail without a checkpoint cannot establish portable history.
    return Checkpoint(None, "") if offset or corrupt_tail else None
