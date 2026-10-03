"""Measure request content without retaining it in profile summaries."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import agentroute_home

# These are structural hints supplied by Codex, not authenticated provenance.
_SECTIONS = re.compile(
    r"(?P<skills_catalog><skills_instructions>.*?</skills_instructions>)"
    r"|(?P<loaded_skills><skill>.*?</skill>)"
    r"|(?P<agents_instructions>\# AGENTS\.md instructions.*?</INSTRUCTIONS>)"
    r"|(?P<memory><memory_instructions>.*?</memory_instructions>"
    r"|(?m:^\#\# Memory\n).*?========= MEMORY_SUMMARY ENDS ========="
    r"|========= MEMORY_SUMMARY BEGINS =========.*?========= MEMORY_SUMMARY ENDS =========)",
    re.DOTALL,
)


def profile_root() -> Path:
    return agentroute_home() / "profiling"


def capture_enabled() -> bool:
    return (
        os.environ.get("AGENTROUTE_PROFILE_CAPTURE") == "1"
        or (profile_root() / "enabled").is_file()
    )


def request_identity(headers: Any, body: dict[str, Any]) -> dict[str, str | None]:
    """Keep only correlation IDs from headers/client metadata, never arbitrary metadata."""
    metadata = body.get("client_metadata") or {}
    try:
        turn = json.loads(headers.get("x-codex-turn-metadata") or "{}")
    except (ValueError, TypeError):
        turn = {}
    if not isinstance(metadata, dict):
        metadata = {}
    if not isinstance(turn, dict):
        turn = {}

    def identifier(value: Any) -> str | None:
        return (
            value
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value)
            else None
        )

    return {
        "session_id": identifier(headers.get("session-id") or metadata.get("session_id")),
        "thread_id": identifier(headers.get("thread-id") or metadata.get("thread_id")),
        "turn_id": identifier(turn.get("turn_id") or metadata.get("turn_id")),
        "root_turn_id": identifier(turn.get("root_turn_id") or metadata.get("root_turn_id")),
        "inference_call_id": identifier(headers.get("x-codex-inference-call-id")),
    }


def count(value: object) -> int:
    """Provider counters must be finite nonnegative integers; bool is not a counter."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, float) and math.isfinite(value) and value >= 0 and value.is_integer():
        return int(value)
    return 0


def normalize_anthropic_usage(raw: dict[str, Any]) -> dict[str, int]:
    """Anthropic input excludes cache reads/writes; Responses input includes them."""
    fresh = count(raw.get("input_tokens"))
    read = count(raw.get("cache_read_input_tokens"))
    write = count(raw.get("cache_creation_input_tokens"))
    output = count(raw.get("output_tokens"))
    ttl = raw.get("cache_creation") or {}
    if not isinstance(ttl, dict):
        ttl = {}

    return {
        "input_tokens": fresh + read + write,
        "uncached_input_tokens": fresh,
        "cached_input_tokens": read,
        "cache_write_input_tokens": write,
        "cache_write_5m_input_tokens": count(ttl.get("ephemeral_5m_input_tokens")),
        "cache_write_1h_input_tokens": count(ttl.get("ephemeral_1h_input_tokens")),
        "output_tokens": output,
        "total_tokens": fresh + read + write + output,
    }


def summarize_context(payload: dict[str, Any], *, anthropic: bool = False) -> dict[str, Any]:
    """Exact UTF-8 content sizes; bytes/4 is only a text-token estimate.

    Media and opaque provider state are counted separately and never tokenized as
    base64 text. Provider-reported input tokens remain the measured context load.
    """

    def has_cache_control(value: Any) -> bool:
        if isinstance(value, dict):
            return "cache_control" in value or any(has_cache_control(v) for v in value.values())
        return isinstance(value, list) and any(has_cache_control(v) for v in value)

    parts: dict[str, dict[str, int]] = {}
    details: dict[tuple[str, str], int] = {}
    current_label = ""
    tool_names: dict[str, str] = {}

    def safe_name(value: Any) -> str:
        return (
            value
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", value)
            else ""
        )

    def find_calls(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                find_calls(child)
        elif isinstance(value, dict):
            if value.get("type") in ("tool_use", "function_call", "custom_tool_call"):
                tool_names[str(value.get("call_id") or value.get("id") or "")] = safe_name(
                    value.get("name")
                )
            for child in value.values():
                if isinstance(child, (dict, list)):
                    find_calls(child)

    find_calls(payload.get("messages" if anthropic else "input", []))
    media = {"images": 0, "audio": 0, "encoded_bytes": 0, "opaque_state_bytes": 0}

    def add(category: str, size: int, label: str = "") -> None:
        if not size:
            return
        p = parts.setdefault(category, {"bytes": 0, "items": 0, "largest_item_bytes": 0})
        p["bytes"] += size
        p["items"] += 1
        p["largest_item_bytes"] = max(p["largest_item_bytes"], size)
        label = label or current_label
        if label:
            details[(category, label)] = details.get((category, label), 0) + size

    def text(value: str, category: str) -> None:
        pos = 0
        for match in _SECTIONS.finditer(value):
            add(category, len(value[pos : match.start()].encode("utf-8")))
            name = re.search(r"<name>([^<]+)</name>", match.group())
            label = safe_name(name[1]) if name and match.lastgroup == "loaded_skills" else ""
            add(str(match.lastgroup), len(match.group().encode("utf-8")), label)
            pos = match.end()
        add(category, len(value[pos:].encode("utf-8")))

    def content(value: Any, category: str) -> None:
        nonlocal current_label
        if isinstance(value, str):
            text(value, category)
        elif isinstance(value, list):
            for item in value:
                content(item, category)
        elif isinstance(value, dict):
            kind = value.get("type", "")
            if kind in ("image", "input_image", "image_url", "input_audio", "audio"):
                media["audio" if "audio" in kind else "images"] += 1
                media["encoded_bytes"] += len(json.dumps(value).encode("utf-8"))
            elif "text" in value:
                content(value["text"], category)
            elif kind == "tool_result":
                previous_label = current_label
                current_label = tool_names.get(str(value.get("tool_use_id")), "")
                content(value.get("content"), "tool_results")
                current_label = previous_label
            elif kind == "tool_use":
                add(
                    "tool_calls",
                    len(json.dumps(value.get("input", {})).encode("utf-8")),
                    safe_name(value.get("name")),
                )
            elif kind == "thinking":
                content(value.get("thinking", ""), "reasoning_history")
            else:
                for key, item in value.items():
                    if key not in {"type", "id", "tool_use_id", "call_id", "signature"}:
                        content(item, category)

    if anthropic:
        content(payload.get("system", []), "base_instructions")
        for message in payload.get("messages", []):
            content(
                message.get("content", []),
                ("assistant_history" if message.get("role") == "assistant" else "user_messages"),
            )
    else:
        content(payload.get("instructions", ""), "base_instructions")
        for item in (
            [payload["input"]]
            if isinstance(payload.get("input"), str)
            else payload.get("input", [])
        ):
            if not isinstance(item, dict):
                content(item, "user_messages")
                continue
            kind = item.get("type")
            if kind == "message" or "role" in item:
                category = {
                    "system": "base_instructions",
                    "developer": "developer_instructions",
                    "assistant": "assistant_history",
                }.get(item.get("role"), "user_messages")
                content(item.get("content", []), category)
            elif kind in ("function_call_output", "custom_tool_call_output"):
                current_label = tool_names.get(str(item.get("call_id")), "")
                content(item.get("output", ""), "tool_results")
                current_label = ""
            elif kind in ("function_call", "custom_tool_call"):
                current_label = safe_name(item.get("name"))
                content(item.get("arguments", item.get("input", "")), "tool_calls")
                current_label = ""
            elif kind in ("reasoning", "compaction"):
                content(item.get("summary", []), "reasoning_history")
                content(item.get("content", []), "reasoning_history")
                media["opaque_state_bytes"] += len(
                    str(item.get("encrypted_content") or "").encode("utf-8")
                )
            else:
                content(item, "other_input")
    tools = payload.get("tools", [])
    for tool in tools:
        if isinstance(tool, dict):
            add(
                "tool_definitions",
                len(json.dumps(tool).encode("utf-8")),
                safe_name(tool.get("name")),
            )
    if payload.get("text"):
        add("response_format", len(json.dumps(payload["text"]).encode("utf-8")))
    for part in parts.values():
        part["estimated_tokens"] = (part["bytes"] + 3) // 4
    return {
        "components": parts,
        "largest_named_items": [
            {
                "category": category,
                "name": label,
                "bytes": size,
                "estimated_tokens": (size + 3) // 4,
            }
            for (category, label), size in sorted(
                details.items(), key=lambda item: item[1], reverse=True
            )[:20]
        ],
        "content_bytes": sum(p["bytes"] for p in parts.values()),
        "serialized_bytes": len(json.dumps(payload).encode("utf-8")),
        "estimated_text_tokens": sum(p["estimated_tokens"] for p in parts.values()),
        "token_estimator": "utf8_bytes_div4; excludes media and opaque state",
        "media": media,
        "cache_control_present": has_cache_control(payload),
        "message_count": len(payload.get("messages" if anthropic else "input", [])),
        "tool_count": len(tools),
    }


def record_bridge_profile(record: dict[str, Any]) -> None:
    """Atomic, private, counts-only sidecar. Telemetry failure cannot fail inference."""
    from .storage import periodic_prune

    periodic_prune()
    temporary: str | None = None
    try:
        profile_root().mkdir(parents=True, exist_ok=True, mode=0o700)
        profile_root().chmod(0o700)
        root = profile_root() / "bridge" / datetime.now(timezone.utc).strftime("%Y-%m-%d")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False, encoding="utf-8") as f:
            temporary = f.name
            json.dump(record, f, ensure_ascii=True)
        # response_id is generated locally, but avoid using identifiers as paths.
        Path(temporary).replace(root / (Path(temporary).name + ".json"))
        temporary = None
    except (OSError, TypeError, ValueError):
        pass
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass
