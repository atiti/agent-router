"""Join Codex request traces with counts-only provider receipts for local profiling."""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import PricingConfig, data_dir
from .context_profile import capture_enabled, count, profile_root, summarize_context
from .pricing import canonical_model

_USAGE_KEYS = (
    "input_tokens",
    "uncached_input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "cache_write_5m_input_tokens",
    "cache_write_1h_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def set_capture(enabled: bool) -> None:
    root = profile_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    marker = root / "enabled"
    if enabled:
        marker.touch(mode=0o600)
        marker.chmod(0o600)
        traces = root / "traces"
        traces.mkdir(exist_ok=True, mode=0o700)
        traces.chmod(0o700)
    else:
        marker.unlink(missing_ok=True)


def capture_status() -> dict[str, Any]:
    root = profile_root()
    from .storage import BRIDGE_LIMIT, TRACE_LIMIT, file_sizes

    return {
        "enabled": capture_enabled(),
        "raw_trace_bytes": file_sizes(root / "traces")[0],
        "raw_trace_limit_bytes": TRACE_LIMIT,
        "bridge_limit_bytes": BRIDGE_LIMIT,
        "retention_days": {"traces": 7, "bridge": 30},
        "root": str(root),
        "trace_bundles": sum(1 for _ in (root / "traces").rglob("trace.jsonl")),
        "bridge_receipts": sum(1 for _ in (root / "bridge").rglob("*.json")),
        "raw_capture": "Codex traces contain request/response text; bridge receipts contain counts",
        "activation": "Codex tracing applies to new sessions launched through AgentRoute",
    }


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def _payload(bundle: Path, ref: Any) -> dict[str, Any]:
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        raise ValueError("missing payload reference")
    path = (bundle / ref["path"]).resolve()
    # Never follow payload references or symlinks outside a bundle.
    if not path.is_relative_to(bundle.resolve()):
        raise ValueError("payload outside bundle")
    return _json(path)


def _native_usage(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict) or "input_tokens" not in raw:
        return None
    result = {key: count(raw.get(key)) for key in _USAGE_KEYS}
    result["uncached_input_tokens"] = max(
        0,
        result["input_tokens"] - result["cached_input_tokens"] - result["cache_write_input_tokens"],
    )
    result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def read_rollout_receipts(
    path: Path, since_ms: int = 0, until_ms: int | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Recover per-response receipts skipped by inference traces (e.g. compaction).

    These receipts do not carry an observed model or assembled request. Never
    attribute them to the turn's default model as if that proved the actual route.
    """
    records: dict[str, dict[str, Any]] = {}
    errors = 0
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                    if event.get("type") != "token_usage_record":
                        continue
                    timestamp = datetime.fromisoformat(event["timestamp"])
                    millis = timestamp.timestamp() * 1000
                    if millis < since_ms or (until_ms is not None and millis > until_ms):
                        continue
                    payload = event["payload"]
                    response_id = payload.get("response_id")
                    if not response_id or not isinstance(payload.get("usage"), dict):
                        continue
                    records[response_id] = {
                        "version": 1,
                        "source": "codex_rollout_receipt",
                        "response_id": response_id,
                        "timestamp": timestamp.isoformat(),
                        "session_id": payload.get("session_id"),
                        "thread_id": payload.get("thread_id"),
                        "turn_id": payload.get("turn_id"),
                        "root_turn_id": payload.get("root_turn_id"),
                        "model": "unknown_untraced_model",
                        "provider": "unknown",
                        "outcome": "completed",
                        "usage": _native_usage(payload["usage"]),
                        "context": {},
                        "context_coverage": "usage_only_context_unavailable",
                    }
                except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
                    errors += 1
    except OSError:
        errors += 1
    return list(records.values()), errors


def read_traces(root: Path) -> tuple[list[dict[str, Any]], int]:
    """Read complete and interrupted attempts, reconstructing websocket continuations.

    Usage is per inference, never summed from repeated cumulative turn snapshots.
    Only size summaries escape this reader. Provider errors and prompt text do not.
    """
    records: list[dict[str, Any]] = []
    errors = 0
    for log in sorted(root.rglob("trace.jsonl")):
        attempts: dict[str, dict[str, Any]] = {}
        rollout_paths: list[tuple[Path, int]] = []
        agents: dict[str, str] = {}
        ended_ms: int | None = None
        # A response inherits the full request input plus the returned assistant items.
        history: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            handle = log.open(encoding="utf-8")
        except OSError:
            errors += 1
            continue
        with handle:
            for line in handle:
                try:
                    event = json.loads(line)
                    payload = event["payload"]
                    kind = payload.get("type")
                    call = payload.get("inference_call_id") or payload.get("compaction_request_id")
                    if kind == "thread_started" and payload.get("metadata_payload"):
                        metadata = _payload(log.parent, payload["metadata_payload"])
                        agents[str(payload.get("thread_id") or event.get("thread_id"))] = str(
                            payload.get("agent_path") or metadata.get("agent_path") or ""
                        )
                        path = metadata.get("rollout_path")
                        if (
                            isinstance(path, str)
                            and Path(path).is_absolute()
                            and path.endswith(".jsonl")
                        ):
                            rollout_paths.append((Path(path), event["wall_time_unix_ms"]))
                    elif kind == "rollout_ended":
                        ended_ms = event["wall_time_unix_ms"]
                    elif kind in {"inference_started", "compaction_request_started"}:
                        request = _payload(log.parent, payload["request_payload"])
                        thread = str(payload.get("thread_id") or event.get("thread_id") or "")
                        previous = request.get("previous_response_id")
                        full = copy.deepcopy(request)
                        coverage = "full_request"
                        if previous:
                            prior = history.get((thread, previous))
                            if prior is None:
                                coverage = "partial_missing_previous_response"
                            else:
                                full = {**copy.deepcopy(prior), **full}
                                full["input"] = prior.get("input", []) + request.get("input", [])
                                coverage = (
                                    "partial_missing_previous_response"
                                    if prior.get("_profile_coverage", "").startswith("partial")
                                    else "reconstructed_websocket"
                                )
                        timestamp = datetime.fromtimestamp(
                            event["wall_time_unix_ms"] / 1000, timezone.utc
                        ).isoformat()
                        measured = summarize_context(
                            {k: v for k, v in full.items() if k != "_profile_coverage"}
                        )
                        full["_profile_coverage"] = coverage
                        record = {
                            "version": 1,
                            "source": "codex_trace",
                            "purpose": "compaction"
                            if kind.startswith("compaction")
                            else "inference",
                            "inference_call_id": call,
                            "session_id": event.get("rollout_id"),
                            "thread_id": thread,
                            "agent_path": agents.get(thread),
                            "turn_id": payload.get("codex_turn_id") or event.get("codex_turn_id"),
                            "model": payload.get("model", ""),
                            "provider": payload.get("provider_name", ""),
                            "timestamp": timestamp,
                            "outcome": "incomplete",
                            "usage": None,
                            "context": measured,
                            "context_coverage": coverage,
                        }
                        attempts[call] = {"record": record, "request": full}
                    elif kind in {
                        "inference_completed",
                        "inference_failed",
                        "inference_cancelled",
                        "compaction_request_completed",
                        "compaction_request_failed",
                    }:
                        attempt = attempts.get(call)
                        if attempt is None:
                            errors += 1
                            continue
                        record = attempt["record"]
                        record["outcome"] = kind.removeprefix("inference_").removeprefix(
                            "compaction_request_"
                        )
                        record["response_id"] = payload.get("response_id")
                        response_ref = payload.get("response_payload") or payload.get(
                            "partial_response_payload"
                        )
                        response = _payload(log.parent, response_ref) if response_ref else {}
                        record["usage"] = _native_usage(response.get("token_usage"))
                        started = datetime.fromisoformat(record["timestamp"]).timestamp() * 1000
                        record["duration_ms"] = max(0, event["wall_time_unix_ms"] - started)
                        if record["response_id"]:
                            full = attempt["request"]
                            full["input"] = full.get("input", []) + response.get("output_items", [])
                            history[(record["thread_id"], record["response_id"])] = full
                except (OSError, ValueError, KeyError, TypeError, OverflowError, AttributeError):
                    # A live trace may end in a partially written line or missing payload.
                    errors += 1
        records.extend(attempt["record"] for attempt in attempts.values())
        for path, since in rollout_paths:
            receipts, receipt_errors = read_rollout_receipts(path, since, ended_ms)
            known = {r.get("response_id") for r in records if r.get("response_id")}
            records.extend(r for r in receipts if r["response_id"] not in known)
            errors += receipt_errors
    return records, errors


def read_profiles(root: Path) -> tuple[list[dict[str, Any]], int]:
    native, errors = read_traces(root / "traces")
    by_call = {r["inference_call_id"]: r for r in native if r.get("inference_call_id")}
    by_response = {r["response_id"]: r for r in native if r.get("response_id")}
    for path in sorted((root / "bridge").rglob("*.json")):
        try:
            bridge = _json(path)
            if bridge.get("source") != "anthropic_bridge" or bridge.get("version") != 1:
                errors += 1
                continue
            matched = by_call.get(bridge.get("inference_call_id")) or by_response.get(
                bridge.get("response_id")
            )
            if matched is not None:
                # The bridge knows the translated provider request and exact cache TTLs.
                ids = {key: matched.get(key) for key in ("session_id", "thread_id", "turn_id")}
                matched.update(bridge)
                for key, value in ids.items():
                    matched[key] = value or matched.get(key)
                matched["source"] = "codex_trace+anthropic_bridge"
                matched["context_coverage"] = "full_provider_request"
            else:
                bridge["context_coverage"] = "full_provider_request"
                native.append(bridge)
        except (OSError, ValueError, TypeError):
            errors += 1
    return native, errors


def read_classifier_receipts(path: Path, cutoff: datetime) -> tuple[list[dict[str, Any]], int]:
    """Open existing audit data read-only; do not initialize or migrate the database."""
    if not path.is_file():
        return [], 0
    records = []
    errors = 0
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT id, created_at, session_id, turn_id, classifier_usage, selection_receipt "
                "FROM routing_decisions WHERE created_at >= ? ORDER BY id",
                (cutoff.isoformat(),),
            )
            for row in rows:
                try:
                    raw = json.loads(row["classifier_usage"] or "{}")
                    if not isinstance(raw, dict) or not raw:
                        continue
                    if "prompt_tokens" not in raw and "input_tokens" not in raw:
                        continue
                    receipt = json.loads(row["selection_receipt"] or "{}")
                    classifier = receipt.get("classifier", {})
                    details = raw.get("prompt_tokens_details") or {}
                    usage = _native_usage(
                        {
                            "input_tokens": raw.get("prompt_tokens", raw.get("input_tokens", 0)),
                            "cached_input_tokens": details.get("cached_tokens", 0),
                            "output_tokens": raw.get(
                                "completion_tokens", raw.get("output_tokens", 0)
                            ),
                        }
                    )
                    records.append(
                        {
                            "version": 1,
                            "source": "routing_classifier_audit",
                            "timestamp": row["created_at"],
                            "session_id": row["session_id"],
                            "thread_id": row["session_id"],
                            "turn_id": row["turn_id"],
                            "inference_call_id": f"classifier-audit-{row['id']}",
                            "model": classifier.get("model", "unknown"),
                            "provider": "routing_classifier",
                            "usage": usage,
                            "outcome": "completed",
                            "context": {},
                            "context_coverage": "usage_only_context_unavailable",
                        }
                    )
                except (ValueError, TypeError, AttributeError):
                    errors += 1
    except sqlite3.Error:
        errors += 1
    return records, errors


def price_usage(record: dict[str, Any], pricing: PricingConfig) -> dict[str, Any]:
    """API-equivalent estimate; subscription quota weights are not public token prices."""
    model = canonical_model(str(record.get("model", "")), pricing)
    rate = pricing.models.get(model)
    raw = record.get("usage")
    if rate is None or not isinstance(raw, dict):
        return {"priced": False, "total_usd": None, "reason": "no price or no usage receipt"}
    usage = {key: count(raw.get(key)) for key in _USAGE_KEYS}
    write = usage["cache_write_input_tokens"]
    one_hour = min(write, usage["cache_write_1h_input_tokens"])
    five_minute = min(write - one_hour, usage["cache_write_5m_input_tokens"])
    unknown = write - one_hour - five_minute
    uncached = max(0, usage["input_tokens"] - usage["cached_input_tokens"] - write)
    write_rate = (
        rate.cache_write_per_million
        if rate.cache_write_per_million is not None
        else rate.input_per_million
    )
    assumptions = []
    if unknown:
        assumptions.append("cache write TTL missing; using configured default write rate")
    if one_hour and rate.cache_write_1h_per_million is None:
        return {"priced": False, "total_usd": None, "reason": "1h cache write price missing"}
    multiplier = (
        rate.long_context_multiplier
        if rate.long_context_threshold_tokens is not None
        and usage["input_tokens"] > rate.long_context_threshold_tokens
        else 1
    )
    components = {
        "uncached_input": uncached * rate.input_per_million * multiplier / 1_000_000,
        "cache_read": usage["cached_input_tokens"]
        * rate.cached_input_per_million
        * multiplier
        / 1_000_000,
        "cache_write_5m": five_minute * write_rate * multiplier / 1_000_000,
        "cache_write_1h": one_hour
        * (rate.cache_write_1h_per_million or 0)
        * multiplier
        / 1_000_000,
        "cache_write_unknown_ttl": unknown * write_rate * multiplier / 1_000_000,
        "output": usage["output_tokens"] * rate.output_per_million * multiplier / 1_000_000,
    }
    return {
        "priced": True,
        "total_usd": sum(components.values()),
        "components_usd": components,
        "pricing_model": model,
        "rates_per_million": rate.model_dump(),
        "assumptions": assumptions,
        "basis": "configured API-equivalent USD estimate",
    }


def profile_report(
    pricing: PricingConfig,
    *,
    root: Path | None = None,
    days: float = 1,
    session: str = "",
    turn: str = "",
    audit_db: Path | None = None,
    rollout: Path | None = None,
) -> dict[str, Any]:
    root = root or profile_root()
    rows, errors = read_profiles(root)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    classifiers, classifier_errors = read_classifier_receipts(
        audit_db
        or (root.parent / "audit.db" if root != profile_root() else data_dir() / "audit.db"),
        cutoff,
    )
    rows.extend(classifiers)
    errors += classifier_errors
    if rollout:
        receipts, rollout_errors = read_rollout_receipts(rollout, int(cutoff.timestamp() * 1000))
        known = {r.get("response_id") for r in rows if r.get("response_id")}
        rows.extend(r for r in receipts if r["response_id"] not in known)
        errors += rollout_errors
    selected = []
    for row in rows:
        try:
            timestamp = datetime.fromisoformat(row["timestamp"])
            if timestamp.tzinfo is None:
                raise ValueError("timestamp has no timezone")
            if timestamp < cutoff:
                continue
            if session and session not in {row.get("session_id"), row.get("thread_id")}:
                continue
            if turn and turn != row.get("turn_id"):
                continue
        except (KeyError, ValueError, TypeError):
            errors += 1
            continue
        row["cost"] = price_usage(row, pricing)
        row["usage_complete"] = row.get("outcome") == "completed" and isinstance(
            row.get("usage"), dict
        )
        selected.append(row)
    selected.sort(key=lambda r: r["timestamp"])
    totals = {key: 0 for key in _USAGE_KEYS}
    costs: dict[str, float] = {}
    models: dict[str, dict[str, Any]] = {}
    exposure: dict[str, int] = {}
    threads: dict[str, dict[str, Any]] = {}
    for row in selected:
        model = str(row.get("model", "unknown"))
        group = models.setdefault(
            model,
            {
                "model": model,
                "requests": 0,
                "measured_requests": 0,
                "tokens": {key: 0 for key in _USAGE_KEYS},
                "priced_usd": 0.0,
                "unpriced_requests": 0,
            },
        )
        group["unpriced_requests"] += not row["cost"]["priced"]
        group["requests"] += 1
        thread = str(row.get("thread_id") or row.get("session_id") or "unknown")
        thread_group = threads.setdefault(
            thread,
            {
                "thread_id": thread,
                "agent_path": row.get("agent_path"),
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "max_input_tokens": 0,
                "first_input_tokens": None,
                "last_input_tokens": None,
                "priced_usd": 0.0,
            },
        )
        thread_group["requests"] += 1
        usage = row.get("usage")
        if isinstance(usage, dict):
            group["measured_requests"] += 1
            if row["source"] != "routing_classifier_audit":
                input_count = count(usage.get("input_tokens"))
                thread_group["input_tokens"] += input_count
                thread_group["output_tokens"] += count(usage.get("output_tokens"))
                thread_group["max_input_tokens"] = max(
                    thread_group["max_input_tokens"], input_count
                )
                if thread_group["first_input_tokens"] is None:
                    thread_group["first_input_tokens"] = input_count
                thread_group["last_input_tokens"] = input_count
            for key in _USAGE_KEYS:
                value = count(usage.get(key))
                totals[key] += value
                group["tokens"][key] += value
        for key, value in row["cost"].get("components_usd", {}).items():
            costs[key] = costs.get(key, 0) + value
            group["priced_usd"] += value
            thread_group["priced_usd"] += value
        for key, component in row.get("context", {}).get("components", {}).items():
            exposure[key] = exposure.get(key, 0) + count(component.get("bytes"))
    observations = []
    for row in selected:
        for suffix in ("_before", ""):
            windows = row.get("subscription_windows" + suffix)
            if windows:
                observations.append(
                    {
                        "timestamp": row.get("subscription_sample" + suffix + "_at")
                        if suffix
                        else row.get("subscription_sample_at"),
                        "request_timestamp": row["timestamp"],
                        "windows": windows,
                        "response_id": row.get("response_id"),
                    }
                )
    largest = sorted(
        selected, key=lambda r: count((r.get("usage") or {}).get("input_tokens")), reverse=True
    )[:10]
    captured_contexts = [r for r in selected if r.get("context", {}).get("components")]
    largest_context = max(
        captured_contexts,
        key=lambda r: (
            count((r.get("usage") or {}).get("input_tokens"))
            or count(r["context"].get("estimated_text_tokens"))
        ),
        default=None,
    )
    return {
        "version": 1,
        "root": str(root),
        "days": days,
        "requests_count": len(selected),
        "classifier_requests": sum(r["source"] == "routing_classifier_audit" for r in selected),
        "subscription_observations": observations,
        "measured_requests": sum(isinstance(r.get("usage"), dict) for r in selected),
        "complete_usage_requests": sum(r["usage_complete"] for r in selected),
        "unpriced_requests": sum(not r["cost"]["priced"] for r in selected),
        "read_errors": errors,
        "tokens": totals,
        "cost_components_usd": costs,
        "priced_usd": sum(costs.values()),
        "models": list(models.values()),
        "threads": list(threads.values()),
        "largest_context_request": largest_context,
        "latest_context_request": captured_contexts[-1] if captured_contexts else None,
        "context_exposure_bytes": exposure,
        "largest_requests": largest,
        "requests": selected,
        "notes": [
            "Input includes cache reads/writes; cache still occupies model context.",
            "USD is an API-equivalent estimate from configured prices, not a subscription bill.",
            "Component sizes are measured bytes; component tokens are bytes/4 estimates.",
            "Context exposure sums repeated content across requests; it is not live context size.",
            "Missing/partial usage receipts mean spend may be higher than measured totals.",
            "Subscription snapshots show observed utilization, not the private quota formula.",
            "Routing classifier receipts are included when recorded; their context is unavailable.",
        ],
    }
