from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import data_dir
from .models import RouteDecision, Tier

SCHEMA = """
CREATE TABLE IF NOT EXISTS routing_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    provider TEXT NOT NULL,
    current_tier TEXT NOT NULL,
    selected_tier TEXT NOT NULL,
    model TEXT NOT NULL,
    reasoning_effort TEXT,
    confidence REAL NOT NULL,
    raw_score REAL NOT NULL,
    reason_codes TEXT NOT NULL,
    contributions TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    prompt TEXT,
    manual_override INTEGER NOT NULL,
    inherited INTEGER NOT NULL,
    switched INTEGER NOT NULL,
    classifier_version TEXT NOT NULL DEFAULT 'legacy',
    classification_source TEXT NOT NULL DEFAULT 'heuristic',
    classifier_confidence REAL,
    classifier_task_type TEXT,
    classifier_reason_hash TEXT,
    proposed_tier TEXT,
    comparison_tier TEXT,
    task_context_used INTEGER NOT NULL DEFAULT 0,
    previous_context_sent INTEGER NOT NULL DEFAULT 0,
    resolved_task_inherited INTEGER NOT NULL DEFAULT 0,
    classifier_latency_ms REAL,
    classifier_request_hash TEXT,
    classifier_usage TEXT NOT NULL DEFAULT '{}',
    risk_floor_applied INTEGER NOT NULL DEFAULT 0,
    agent_requested_tier TEXT,
    agent_request_reason_hash TEXT,
    selection_receipt TEXT NOT NULL DEFAULT '{}',
    selection_receipt_hash TEXT,
    outcome_label TEXT,
    outcome_notes TEXT,
    answer_model TEXT,
    answer_input_tokens INTEGER,
    answer_cached_input_tokens INTEGER,
    answer_cache_write_input_tokens INTEGER,
    answer_output_tokens INTEGER,
    answer_reasoning_output_tokens INTEGER,
    answer_total_tokens INTEGER,
    usage_recorded_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_routing_session
ON routing_decisions(session_id, id DESC);
"""

MIGRATIONS = {
    "turn_id": "TEXT",
    "classifier_version": "TEXT NOT NULL DEFAULT 'legacy'",
    "classification_source": "TEXT NOT NULL DEFAULT 'heuristic'",
    "classifier_confidence": "REAL",
    "classifier_task_type": "TEXT",
    "classifier_reason_hash": "TEXT",
    "proposed_tier": "TEXT",
    "comparison_tier": "TEXT",
    "task_context_used": "INTEGER NOT NULL DEFAULT 0",
    "previous_context_sent": "INTEGER NOT NULL DEFAULT 0",
    "resolved_task_inherited": "INTEGER NOT NULL DEFAULT 0",
    "classifier_latency_ms": "REAL",
    "classifier_request_hash": "TEXT",
    "classifier_usage": "TEXT NOT NULL DEFAULT '{}'",
    "risk_floor_applied": "INTEGER NOT NULL DEFAULT 0",
    "agent_requested_tier": "TEXT",
    "agent_request_reason_hash": "TEXT",
    "selection_receipt": "TEXT NOT NULL DEFAULT '{}'",
    "selection_receipt_hash": "TEXT",
    "outcome_label": "TEXT",
    "outcome_notes": "TEXT",
    "answer_model": "TEXT",
    "answer_input_tokens": "INTEGER",
    "answer_cached_input_tokens": "INTEGER",
    "answer_cache_write_input_tokens": "INTEGER",
    "answer_output_tokens": "INTEGER",
    "answer_reasoning_output_tokens": "INTEGER",
    "answer_total_tokens": "INTEGER",
    "usage_recorded_at": "TEXT",
}


class AuditStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / "audit.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(routing_decisions)").fetchall()
            }
            for name, declaration in MIGRATIONS.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE routing_decisions ADD COLUMN {name} {declaration}"
                    )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_routing_turn "
                "ON routing_decisions(session_id, turn_id)"
            )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def record(
        self,
        decision: RouteDecision,
        current_tier: Tier,
        prompt: str | None = None,
    ) -> int:
        with self.connection() as connection:
            previous = connection.execute(
                "SELECT * FROM routing_decisions WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (decision.session_id,),
            ).fetchone()
            if (
                decision.manual_override
                and previous is not None
                and not previous["manual_override"]
                and previous["outcome_label"] is None
            ):
                connection.execute(
                    "UPDATE routing_decisions SET outcome_label = ?, outcome_notes = ? "
                    "WHERE id = ?",
                    (
                        "overridden",
                        f"next turn manually selected {decision.tier}",
                        previous["id"],
                    ),
                )
            cursor = connection.execute(
                """
                INSERT INTO routing_decisions (
                    created_at, session_id, turn_id, provider, current_tier, selected_tier,
                    model, reasoning_effort, confidence, raw_score, reason_codes,
                    contributions, prompt_hash, prompt, manual_override, inherited, switched,
                    classifier_version, proposed_tier, comparison_tier, task_context_used,
                    previous_context_sent, resolved_task_inherited, classifier_latency_ms,
                    classifier_request_hash, classifier_usage, risk_floor_applied,
                    agent_requested_tier, agent_request_reason_hash,
                    classification_source, classifier_confidence, classifier_task_type,
                    classifier_reason_hash, selection_receipt, selection_receipt_hash
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    decision.session_id,
                    decision.turn_id,
                    decision.provider,
                    str(current_tier),
                    str(decision.tier),
                    decision.model,
                    decision.reasoning_effort,
                    decision.confidence,
                    decision.raw_score,
                    json.dumps([code.value for code in decision.reason_codes]),
                    json.dumps([item.model_dump(mode="json") for item in decision.contributions]),
                    decision.prompt_hash,
                    prompt,
                    int(decision.manual_override),
                    int(decision.inherited),
                    int(decision.switched),
                    decision.classifier_version,
                    str(decision.proposed_tier) if decision.proposed_tier is not None else None,
                    str(decision.comparison_tier) if decision.comparison_tier is not None else None,
                    int(decision.task_context_used),
                    int(decision.previous_context_sent),
                    int(decision.resolved_task_inherited),
                    decision.classifier_latency_ms,
                    decision.classifier_request_hash,
                    json.dumps(decision.classifier_usage, sort_keys=True, separators=(",", ":")),
                    int(decision.risk_floor_applied),
                    (
                        str(decision.agent_requested_tier)
                        if decision.agent_requested_tier is not None
                        else None
                    ),
                    decision.agent_request_reason_hash,
                    decision.classification_source,
                    decision.classifier_confidence,
                    decision.classifier_task_type,
                    decision.classifier_reason_hash,
                    json.dumps(decision.selection_receipt, sort_keys=True, separators=(",", ":")),
                    decision.selection_receipt_hash,
                ),
            )
            return int(cursor.lastrowid)

    def record_usage(
        self, session_id: str, turn_id: str, model: str, usage: dict[str, int]
    ) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE routing_decisions SET
                    answer_model = ?, answer_input_tokens = ?,
                    answer_cached_input_tokens = ?, answer_cache_write_input_tokens = ?,
                    answer_output_tokens = ?, answer_reasoning_output_tokens = ?,
                    answer_total_tokens = ?, usage_recorded_at = ?
                WHERE id = (
                    SELECT id FROM routing_decisions
                    WHERE session_id = ? AND turn_id = ? ORDER BY id DESC LIMIT 1
                )
                """,
                (
                    model,
                    usage.get("input_tokens", 0),
                    usage.get("cached_input_tokens", 0),
                    usage.get("cache_write_input_tokens", 0),
                    usage.get("output_tokens", 0),
                    usage.get("reasoning_output_tokens", 0),
                    usage.get("total_tokens", 0),
                    datetime.now(timezone.utc).isoformat(),
                    session_id,
                    turn_id,
                ),
            )
            return cursor.rowcount == 1

    def latest(self, session_id: str | None = None) -> sqlite3.Row | None:
        query = "SELECT * FROM routing_decisions"
        params: tuple[object, ...] = ()
        if session_id:
            query += " WHERE session_id = ?"
            params = (session_id,)
        query += " ORDER BY id DESC LIMIT 1"
        with self.connection() as connection:
            return connection.execute(query, params).fetchone()

    def history(self, session_id: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
        query = "SELECT * FROM routing_decisions"
        params: tuple[object, ...]
        if session_id:
            query += " WHERE session_id = ?"
            params = (session_id, limit)
        else:
            params = (limit,)
        query += " ORDER BY id DESC LIMIT ?"
        with self.connection() as connection:
            return list(connection.execute(query, params).fetchall())

    def previous_tier(self, session_id: str) -> Tier | None:
        row = self.latest(session_id)
        return Tier.parse(row["selected_tier"]) if row else None

    def label(self, decision_id: int, outcome: str, notes: str | None = None) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE routing_decisions SET outcome_label = ?, outcome_notes = ? WHERE id = ?",
                (outcome, notes, decision_id),
            )
            return cursor.rowcount == 1
