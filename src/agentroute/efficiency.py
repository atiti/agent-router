"""Read existing Codex receipts; never infer model time from overlapping tool time."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

MAX_RECEIPT_BYTES = 8 * 1024 * 1024


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(value) and value >= 0:
            return float(value)
    return None


def execution_receipt(path: object, turn_id: object) -> dict[str, Any]:
    """Bounded, exact-turn counters from Codex rollout events, without tool payloads.

    Absent event types are unobserved, not evidence of zero work. Tool durations
    are summed work, not elapsed time (tools can overlap). A tail can be partial.
    """
    if not isinstance(path, str) or not isinstance(turn_id, str) or not turn_id:
        return {}
    try:
        with Path(path).expanduser().open("rb") as source:
            size = source.seek(0, 2)
            offset = max(0, size - MAX_RECEIPT_BYTES)
            source.seek(offset)
            data = source.read(MAX_RECEIPT_BYTES)
    except OSError:
        return {}
    lines = data.splitlines()[1 if offset else 0 :]
    responses: dict[str, dict] = {}
    tools: dict[tuple[str, str], dict] = {}
    started = False
    compactions = 0
    progress_reviews: dict[str, dict[str, Any] | None] = {}
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("turn_id") != turn_id:
            continue
        kind = payload.get("type")
        if event.get("type") == "token_usage_record":
            response_id = payload.get("response_id")
            if isinstance(response_id, str) and response_id:
                responses[response_id] = payload
        elif event.get("type") == "event_msg":
            if kind in {"task_started", "turn_started"}:
                started = True
            elif kind == "context_compacted":
                compactions += 1
            elif kind == "item_completed":
                item = payload.get("item")
                if isinstance(item, dict) and str(item.get("id", "")).startswith("goal-review-"):
                    content = item.get("content")
                    if isinstance(content, list):
                        text = "\n".join(
                            part.get("text", "")
                            for part in content
                            if isinstance(part, dict) and isinstance(part.get("text"), str)
                        )
                        if text.startswith("Independent progress review:"):
                            match = re.search(
                                r"\nReviewer usage: input=(\d+) cached=(\d+) output=(\d+)$", text
                            )
                            usage = None
                            if match:
                                input_tokens, cached, output = map(int, match.groups())
                                usage = {
                                    "input_tokens": input_tokens,
                                    "cached_input_tokens": cached,
                                    "output_tokens": output,
                                }
                                route_match = re.search(r"\nReviewer route: (\{[^\n]*\})\n", text)
                                if route_match:
                                    try:
                                        route = json.loads(route_match.group(1))
                                    except ValueError:
                                        route = None
                                    if isinstance(route, dict):
                                        for key in ("model", "provider"):
                                            value = route.get(key)
                                            if isinstance(value, str) and re.fullmatch(
                                                r"[A-Za-z0-9_.@/-]{1,160}", value
                                            ):
                                                usage[key] = value
                                        write = route.get("cache_write_input_tokens")
                                        if (
                                            isinstance(write, int)
                                            and not isinstance(write, bool)
                                            and 0 <= write <= input_tokens
                                        ):
                                            usage["cache_write_input_tokens"] = write
                            progress_reviews[item["id"]] = usage
            elif kind in {"exec_command_end", "mcp_tool_call_end"}:
                call_id = payload.get("call_id")
                if isinstance(call_id, str) and call_id:
                    tools[(kind, call_id)] = payload
    if not responses and not tools and not started and not compactions and not progress_reviews:
        return {}
    durations = []
    failures = 0
    for (kind, _), tool in tools.items():
        duration = tool.get("duration")
        if isinstance(duration, dict):
            secs, nanos = _number(duration.get("secs")), _number(duration.get("nanos"))
            if secs is not None and nanos is not None and nanos < 1_000_000_000:
                durations.append(secs * 1000 + nanos / 1_000_000)
        if kind == "exec_command_end":
            failures += isinstance(tool.get("exit_code"), int) and tool["exit_code"] != 0
        else:
            result = tool.get("result")
            if isinstance(result, dict):
                ok = result.get("Ok")
                failures += "Err" in result or (isinstance(ok, dict) and ok.get("isError") is True)
    totals = {key: 0 for key in ("input_tokens", "cached_input_tokens", "output_tokens")}
    for response in responses.values():
        usage = response.get("usage")
        if isinstance(usage, dict):
            for key in totals:
                totals[key] += int(_number(usage.get(key)) or 0)
    last = next(reversed(responses.values()), {})
    last_usage = last.get("usage")
    last_usage = last_usage if isinstance(last_usage, dict) else {}
    return {
        "version": 1,
        "source": "codex_rollout",
        "coverage": "turn_start_seen" if started else "partial",
        "tail_truncated": bool(offset),
        "response_count": len(responses),
        "observed_tool_completions": len(tools),
        "observed_tool_failures": int(failures),
        "tool_duration_receipts": len(durations),
        "tool_work_ms": sum(durations) if durations else None,
        "observed_compactions": compactions,
        "progress_review_count": len(progress_reviews),
        "progress_review_usage_observed": sum(
            value is not None for value in progress_reviews.values()
        ),
        "progress_review_input_tokens": sum(
            value["input_tokens"] for value in progress_reviews.values() if value is not None
        ),
        "progress_review_cached_input_tokens": sum(
            value["cached_input_tokens"] for value in progress_reviews.values() if value is not None
        ),
        "progress_review_output_tokens": sum(
            value["output_tokens"] for value in progress_reviews.values() if value is not None
        ),
        "progress_review_receipts": [
            value for value in progress_reviews.values() if value is not None
        ],
        "root_turn_id": last.get("root_turn_id"),
        "last_response_usage": {key: int(_number(last_usage.get(key)) or 0) for key in totals},
        **totals,
    }


def efficiency_report(rows: list, pricing: Any = None) -> dict[str, Any]:
    """Aggregate measured receipts and label unavailable timings honestly."""
    receipts = []
    for row in rows:
        try:
            receipt = json.loads(row["execution_receipt"] or "{}")
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if isinstance(receipt, dict) and receipt.get("version") == 1:
            receipts.append(receipt)
    inputs = sum(r.get("input_tokens", 0) for r in receipts)
    cached = sum(r.get("cached_input_tokens", 0) for r in receipts)
    reviews = [
        review
        for receipt in receipts
        for review in receipt.get("progress_review_receipts", [])
        if isinstance(review, dict)
    ]
    from .pricing import canonical_model, token_cost

    priced = [
        review
        for review in reviews
        if pricing is not None
        and isinstance(review.get("model"), str)
        and canonical_model(review["model"], pricing) in pricing.models
        and "cache_write_input_tokens" in review
    ]
    return {
        "measured_turns": len(receipts),
        "unmeasured_turns": len(rows) - len(receipts),
        "partial_turns": sum(r.get("coverage") != "turn_start_seen" for r in receipts),
        "response_count": sum(r.get("response_count", 0) for r in receipts),
        "observed_tool_completions": sum(r.get("observed_tool_completions", 0) for r in receipts),
        "observed_tool_failures": sum(r.get("observed_tool_failures", 0) for r in receipts),
        "tool_work_ms": sum(r.get("tool_work_ms") or 0 for r in receipts),
        "observed_compactions": sum(r.get("observed_compactions", 0) for r in receipts),
        "progress_review_priced_count": len(priced),
        "progress_review_unpriced_or_unobserved_count": sum(
            r.get("progress_review_count", 0) for r in receipts
        )
        - len(priced),
        "progress_review_estimated_cost_usd": sum(
            token_cost(review["model"], review, pricing) for review in priced
        )
        if priced
        else None,
        "cached_input_fraction": min(1.0, cached / inputs) if inputs else None,
        "model_latency_ms": None,
        "note": "Observed tool work can overlap; model latency is not derived by subtraction.",
    }
