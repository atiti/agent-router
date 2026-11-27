"""Read voice replies from the exact conversation that received the instruction."""

from __future__ import annotations

import json
from pathlib import Path

from .config import codex_home
from .dashboard import _epoch, _rows, redact
from .dashboard_packet_routes import thread_id

MAX_REPLY_SCAN = 1_048_576
MAX_REPLY_TEXT = 12_000


def conversation_url(target: object) -> str | None:
    identifier = thread_id(target)
    return f"codex://threads/{identifier}" if identifier else None


def reply_prompt(marker: str) -> str:
    return (
        f'When you have a concrete response to this instruction, start a standalone '
        f'line of your final reply with "[voice-reply {marker}]" followed by your answer.'
    )


def reply_evidence(target: str, marker: str | None, since: float, *,
                   expected_turn: str | None = None, input_prefix: str | None = None) -> dict:
    """Verify receipt-linked output using the turn's terminal event, independent of phase."""
    unavailable = {"reply_state": "output_unconfirmed", "verified_reply": None}
    if not marker or not thread_id(target):
        return unavailable
    if not expected_turn and not input_prefix:
        return {"reply_state": "turn_unconfirmed", "verified_reply": None}
    root = codex_home().resolve()
    rows = _rows(root / "state_5.sqlite", "SELECT rollout_path FROM threads WHERE id = ?",
                 (target,))
    if not rows or not rows[0].get("rollout_path"):
        return unavailable
    try:
        path = Path(rows[0]["rollout_path"]).resolve(strict=True)
        if not any(path.is_relative_to(root / name) for name in ("sessions", "archived_sessions")):
            return unavailable
        with path.open("rb") as stream:
            header = json.loads(stream.readline(65_536))
            if header.get("type") != "session_meta" or header["payload"].get("id") != target:
                return unavailable
            size = stream.seek(0, 2)
            stream.seek(max(0, size - MAX_REPLY_SCAN))
            lines = stream.read(MAX_REPLY_SCAN).split(b"\n")
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        return unavailable

    linked_turns = set()
    observed_turns = set()
    outcomes: dict[str, dict] = {}
    commentary: dict[str, set[str]] = {}
    turn_filter = expected_turn.encode() if expected_turn else None
    for line in lines[1:-1] if size > MAX_REPLY_SCAN else lines[:-1]:
        if b'"event_msg"' not in line or (turn_filter and turn_filter not in line):
            continue
        try:
            record = json.loads(line)
            if record.get("type") != "event_msg":
                continue
            payload = record["payload"]
            turn, kind = payload.get("turn_id"), payload.get("type")
            at = _epoch(record.get("timestamp"))
            if (not isinstance(turn, str) or not turn or at is None or at < since
                    or payload.get("thread_id", target) != target
                    or (expected_turn and turn != expected_turn)):
                continue
            observed_turns.add(turn)
            if kind in {"task_complete", "turn_complete", "turn_aborted"}:
                text = payload.get("last_agent_message")
                has_final = "last_agent_message" in payload
                outcome = (
                    "failed" if payload.get("error") else
                    "interrupted" if kind == "turn_aborted" else
                    "completed" if has_final else "output_unconfirmed"
                )
                entry = {"outcome": outcome, "text": text, "at": at}
                previous = outcomes.get(turn)
                if previous and (previous["outcome"], previous["text"]) != (outcome, text):
                    entry["outcome"] = "output_unconfirmed"
                outcomes[turn] = entry
            elif kind == "item_completed":
                item = payload.get("item") or {}
                parts = item.get("content") or []
                if input_prefix and item.get("type") == "UserMessage" and any(
                    isinstance(part, dict) and isinstance(part.get("text"), str)
                    and part["text"].startswith(input_prefix)
                    for part in parts
                ):
                    linked_turns.add(turn)
                # Codex can copy its last commentary into last_agent_message if
                # the final sampling step produces commentary only.
                if item.get("type") == "AgentMessage" and item.get("phase") == "commentary":
                    message = "".join(part["text"] for part in parts).strip()
                    commentary.setdefault(turn, set()).add(message)
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            continue

    turn = expected_turn
    if not turn:
        if len(linked_turns) != 1:
            return {"reply_state": "turn_unconfirmed", "verified_reply": None}
        turn = next(iter(linked_turns))
    final = outcomes.get(turn)
    state = "pending" if turn in observed_turns else "output_unconfirmed"
    result = {"reply_state": state, "verified_reply": None, "reply_turn_id": turn}
    if not final:
        return result
    state = final["outcome"]
    if state != "completed":
        return {**result, "reply_state": state}
    output = final["text"]
    if output is not None and not isinstance(output, str):
        return {**result, "reply_state": "output_unconfirmed"}
    if output and output.strip() in commentary.get(turn, ()):
        return {**result, "reply_state": "output_unconfirmed"}
    prefix = f"[voice-reply {marker}]"
    lines = (output or "").splitlines()
    matches = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    if len(matches) > 1:
        return {**result, "reply_state": "output_unconfirmed"}
    if not matches:
        return {**result, "reply_state": "completed_without_reply"}
    index = matches[0]
    reply = "\n".join([lines[index][len(prefix):].lstrip(), *lines[index + 1:]]).strip()
    if not reply:
        return {**result, "reply_state": "completed_without_reply"}
    safe_text = redact(reply, MAX_REPLY_TEXT + 1)
    return {
        **result, "reply_state": "replied",
        "verified_reply": {
            "text": safe_text[:MAX_REPLY_TEXT], "at": final["at"], "thread_id": target,
            "turn_id": turn, "truncated": len(safe_text) > MAX_REPLY_TEXT,
        },
    }
