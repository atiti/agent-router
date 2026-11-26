"""Read voice replies from the exact conversation that received the instruction."""

from __future__ import annotations

import json
from pathlib import Path

from .config import codex_home
from .dashboard import _epoch, _rows, redact
from .dashboard_packet_routes import thread_id

MAX_REPLY_SCAN = 1_048_576


def conversation_url(target: object) -> str | None:
    identifier = thread_id(target)
    return f"codex://threads/{identifier}" if identifier else None


def reply_prompt(marker: str) -> str:
    return (
        f'When you have a concrete response to this instruction, start a standalone '
        f'line of your final reply with "[voice-reply {marker}]" followed by your answer.'
    )


def linked_reply(target: str, marker: str | None, since: float) -> dict | None:
    """Only a final assistant message in the target rollout can prove a linked reply."""
    if not marker or not thread_id(target):
        return None
    root = codex_home().resolve()
    rows = _rows(root / "state_5.sqlite", "SELECT rollout_path FROM threads WHERE id = ?",
                 (target,))
    if not rows or not rows[0].get("rollout_path"):
        return None
    prefix = f"[voice-reply {marker}]"
    try:
        path = Path(rows[0]["rollout_path"]).resolve(strict=True)
        if not any(path.is_relative_to(root / name) for name in ("sessions", "archived_sessions")):
            return None
        with path.open("rb") as stream:
            header = json.loads(stream.readline(65_536))
            if header.get("type") != "session_meta" or header["payload"].get("id") != target:
                return None
            size = stream.seek(0, 2)
            stream.seek(max(0, size - MAX_REPLY_SCAN))
            content = stream.read(MAX_REPLY_SCAN)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        return None
    lines = content.split(b"\n")
    for line in reversed(lines[1:-1] if size > MAX_REPLY_SCAN else lines[:-1]):
        if prefix.encode() not in line:
            continue
        try:
            record = json.loads(line)
            payload = record["payload"]
            at = _epoch(record.get("timestamp"))
            if at is None or at < since:
                continue
            if record["type"] == "event_msg" and payload.get("type") == "item_completed":
                item = payload["item"]
                if item.get("type") != "AgentMessage":
                    continue
                turn, item_id = payload.get("turn_id"), item.get("id")
            elif record["type"] == "response_item" and payload.get("type") == "message":
                if payload.get("role") != "assistant":
                    continue
                item = payload
                source = (record.get("metadata") or {}).get("retained_source") or {}
                identity = source.get("id") or {}
                turn = identity.get("turn_id")
                item_id = identity.get("message_id") or item.get("id")
            else:
                continue
            if item.get("phase") != "final_answer":
                continue
            text = "\n".join(part.get("text", "") for part in item.get("content", []))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        for index, paragraph in enumerate(text.splitlines()):
            if paragraph.startswith(prefix):
                reply = "\n".join([paragraph[len(prefix):], *text.splitlines()[index + 1:]])
                if reply.strip():
                    return {"text": redact(reply, 600), "at": at, "thread_id": target,
                            "turn_id": turn, "item_id": item_id}
    return None
