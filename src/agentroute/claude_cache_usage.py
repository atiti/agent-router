"""Private numeric cache receipts, limited to 2,000 requests and 30 days."""

import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import agentroute_home
from .context_profile import count

RECEIPT_LIMIT = 2000
RETENTION_SECONDS = 30 * 86400
COUNTERS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "cache_write_5m_input_tokens",
    "cache_write_1h_input_tokens",
    "output_tokens",
)


def cache_usage_path() -> Path:
    return agentroute_home() / "state" / "claude-cache.sqlite3"


def record_cache_usage(
    *,
    profile: str,
    model: str,
    mode: str,
    ttl: str,
    outcome: str,
    usage: dict[str, Any] | None,
    boundaries: dict[str, int],
    path: Path | None = None,
) -> None:
    """Telemetry failures must never fail a model request or retain its prompt."""
    path = path or cache_usage_path()
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,160}", model):
        model = "unknown"
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", profile):
        profile = "unknown"
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        # DELETE journaling and a short lock timeout bound writes across processes.
        # No prompt, account identity, session ID, or credential enters this database.
        with sqlite3.connect(path, timeout=0.1) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS receipts ("
                "id INTEGER PRIMARY KEY, recorded_at REAL, profile TEXT, model TEXT, "
                "mode TEXT, ttl TEXT, outcome TEXT, measured INTEGER, "
                "input_tokens INTEGER, cached_input_tokens INTEGER, "
                "cache_write_input_tokens INTEGER, cache_write_5m_input_tokens INTEGER, "
                "cache_write_1h_input_tokens INTEGER, output_tokens INTEGER, "
                "tools_bytes INTEGER, system_bytes INTEGER, messages_bytes INTEGER)"
            )
            db.execute(
                "INSERT INTO receipts VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    profile,
                    model,
                    mode,
                    ttl,
                    outcome,
                    int(usage is not None),
                    *(count((usage or {}).get(key)) for key in COUNTERS),
                    *(
                        count(boundaries.get(key))
                        for key in ("tools_bytes", "system_bytes", "messages_bytes")
                    ),
                ),
            )
            db.execute(
                "DELETE FROM receipts WHERE recorded_at < ? OR id NOT IN "
                "(SELECT id FROM receipts ORDER BY id DESC LIMIT ?)",
                (time.time() - RETENTION_SECONDS, RECEIPT_LIMIT),
            )
    except (OSError, sqlite3.Error, OverflowError):
        return


def cache_usage_report(*, days: float = 1, path: Path | None = None) -> dict[str, Any]:
    path = path or cache_usage_path()
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    error = None
    rows = []
    if path.exists():
        try:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    "SELECT * FROM receipts WHERE recorded_at >= ? ORDER BY id",
                    (time.time() - days * 86400,),
                ).fetchall()
        except sqlite3.Error:
            error = "Cache receipts are temporarily unavailable."
    for row in rows:
        group = groups.setdefault(
            (row["profile"], row["model"]),
            {
                "profile": row["profile"],
                "model": row["model"],
                "requests": 0,
                "measured_requests": 0,
                "completed_requests": 0,
                "tokens": {key: 0 for key in COUNTERS},
                "forwarded_prefix_bytes": {key: 0 for key in ("tools_bytes", "system_bytes")},
                "latest_context_bytes": {},
                "latest_mode": row["mode"],
                "latest_ttl": row["ttl"],
            },
        )
        group["requests"] += 1
        group["measured_requests"] += row["measured"]
        group["completed_requests"] += row["outcome"] == "completed"
        for key in COUNTERS:
            group["tokens"][key] += row[key]
        for key in group["forwarded_prefix_bytes"]:
            group["forwarded_prefix_bytes"][key] += row[key]
        group["latest_context_bytes"] = {
            key: row[key] for key in ("tools_bytes", "system_bytes", "messages_bytes")
        }
        group["latest_mode"], group["latest_ttl"] = row["mode"], row["ttl"]
    for group in groups.values():
        tokens = group["tokens"]
        tokens["uncached_input_tokens"] = max(
            0,
            tokens["input_tokens"]
            - tokens["cached_input_tokens"]
            - tokens["cache_write_input_tokens"],
        )
        group["cache_read_percent"] = (
            100 * tokens["cached_input_tokens"] / tokens["input_tokens"]
            if tokens["input_tokens"]
            else None
        )
    return {
        "days": days,
        "retained_requests": len(rows),
        "receipt_limit": RECEIPT_LIMIT,
        "retention_days": 30,
        "groups": list(groups.values()),
        "error": error,
    }
