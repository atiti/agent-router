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
    backend TEXT NOT NULL DEFAULT 'gpt',
    model_provider TEXT NOT NULL DEFAULT 'openai',
    sticky_backend TEXT,
    strip_provider_state INTEGER NOT NULL DEFAULT 0,
    route_scope TEXT NOT NULL DEFAULT 'root',
    agent_id TEXT,
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
    classifier_status TEXT NOT NULL DEFAULT 'skipped',
    classifier_error_type TEXT,
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
    usage_recorded_at TEXT,
    reported_answer_model TEXT,
    answer_model_mismatch INTEGER NOT NULL DEFAULT 0,
    turn_completed_at TEXT,
    turn_duration_ms REAL,
    turn_outcome TEXT NOT NULL DEFAULT 'pending',
    completion_source TEXT,
    usage_status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_routing_session
ON routing_decisions(session_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_routing_created_at
ON routing_decisions(created_at, id ASC);
"""

MIGRATIONS = {
    "turn_id": "TEXT",
    "backend": "TEXT NOT NULL DEFAULT 'gpt'",
    "model_provider": "TEXT NOT NULL DEFAULT 'openai'",
    "sticky_backend": "TEXT",
    "strip_provider_state": "INTEGER NOT NULL DEFAULT 0",
    "route_scope": "TEXT NOT NULL DEFAULT 'root'",
    "agent_id": "TEXT",
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
    "classifier_status": "TEXT NOT NULL DEFAULT 'skipped'",
    "classifier_error_type": "TEXT",
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
    "reported_answer_model": "TEXT",
    "answer_model_mismatch": "INTEGER NOT NULL DEFAULT 0",
    "turn_completed_at": "TEXT",
    "turn_duration_ms": "REAL",
    "turn_outcome": "TEXT NOT NULL DEFAULT 'pending'",
    "completion_source": "TEXT",
    "usage_status": "TEXT NOT NULL DEFAULT 'pending'",
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
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_routing_created_at "
                "ON routing_decisions(created_at, id ASC)"
            )
            # Runtime releases before v23 sent the frozen session-start model to Stop hooks.
            # Preserve that observed value for diagnostics, but attribute usage to the routed
            # model that actually produced the step. This repair is idempotent.
            connection.execute(
                "UPDATE routing_decisions SET reported_answer_model = answer_model, "
                "answer_model_mismatch = 1, answer_model = model "
                "WHERE usage_recorded_at IS NOT NULL AND answer_model IS NOT NULL "
                "AND answer_model != model AND reported_answer_model IS NULL"
            )
            connection.execute(
                "UPDATE routing_decisions SET turn_completed_at = usage_recorded_at, "
                "turn_duration_ms = MAX(0, "
                "(julianday(usage_recorded_at) - julianday(created_at)) * 86400000.0) "
                "WHERE usage_recorded_at IS NOT NULL AND turn_completed_at IS NULL"
            )
            connection.execute(
                "UPDATE routing_decisions SET turn_outcome = 'completed', "
                "completion_source = COALESCE(completion_source, 'legacy_usage'), "
                "usage_status = 'recorded' WHERE usage_recorded_at IS NOT NULL "
                "AND turn_outcome = 'pending'"
            )
            connection.execute(
                "UPDATE routing_decisions SET turn_outcome = 'completed', "
                "completion_source = COALESCE(completion_source, 'stop_hook'), "
                "usage_status = 'missing' WHERE turn_completed_at IS NOT NULL "
                "AND usage_recorded_at IS NULL AND turn_outcome = 'pending'"
            )
            connection.execute(
                "UPDATE routing_decisions SET classifier_status = 'succeeded' "
                "WHERE classification_source IN ('local_llm', 'private_llm', 'cloud_llm') "
                "AND classifier_status = 'skipped'"
            )
            connection.execute(
                "UPDATE routing_decisions SET classifier_status = 'fallback' "
                "WHERE classification_source = 'heuristic_fallback' "
                "AND classifier_status = 'skipped'"
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
                    created_at, session_id, turn_id, provider, backend, model_provider,
                    route_scope, agent_id, sticky_backend, strip_provider_state,
                    current_tier, selected_tier,
                    model, reasoning_effort, confidence, raw_score, reason_codes,
                    contributions, prompt_hash, prompt, manual_override, inherited, switched,
                    classifier_version, proposed_tier, comparison_tier, task_context_used,
                    previous_context_sent, resolved_task_inherited, classifier_latency_ms,
                    classifier_request_hash, classifier_usage, classifier_status,
                    classifier_error_type, risk_floor_applied,
                    agent_requested_tier, agent_request_reason_hash,
                    classification_source, classifier_confidence, classifier_task_type,
                    classifier_reason_hash, selection_receipt, selection_receipt_hash
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    decision.session_id,
                    decision.turn_id,
                    decision.provider,
                    decision.backend,
                    decision.model_provider,
                    decision.route_scope,
                    decision.agent_id,
                    decision.sticky_backend,
                    int(decision.strip_provider_state),
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
                    decision.classifier_status,
                    decision.classifier_error_type,
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

    def record_completion(
        self,
        session_id: str,
        turn_id: str,
        model: str,
        usage: dict[str, int] | None = None,
        outcome: str = "completed",
        completion_source: str = "stop_hook",
    ) -> bool:
        """Record first completion time and optional usage for one exact routed turn."""
        completed_at = datetime.now(timezone.utc)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id, created_at, model, turn_completed_at, turn_duration_ms "
                "FROM routing_decisions WHERE session_id = ? AND turn_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (session_id, turn_id),
            ).fetchone()
            if row is None:
                return False
            routed_model = str(row["model"])
            reported_model = model or None
            mismatch = bool(reported_model and reported_model != routed_model)
            first_completed_at = row["turn_completed_at"] or completed_at.isoformat()
            if row["turn_duration_ms"] is not None:
                duration_ms = float(row["turn_duration_ms"])
            else:
                started_at = datetime.fromisoformat(str(row["created_at"]))
                duration_ms = max(
                    0.0,
                    (completed_at - started_at.astimezone(timezone.utc)).total_seconds() * 1000,
                )
            cursor = connection.execute(
                """
                UPDATE routing_decisions SET
                    answer_model = ?,
                    reported_answer_model = ?, answer_model_mismatch = ?,
                    turn_completed_at = ?, turn_duration_ms = ?,
                    turn_outcome = ?, completion_source = ?, usage_status = ?
                WHERE id = ?
                """,
                (
                    routed_model,
                    reported_model,
                    int(mismatch),
                    first_completed_at,
                    duration_ms,
                    outcome,
                    completion_source,
                    "recorded" if usage is not None else "missing",
                    row["id"],
                ),
            )
            if usage is not None:
                connection.execute(
                    """
                    UPDATE routing_decisions SET
                        answer_input_tokens = ?, answer_cached_input_tokens = ?,
                        answer_cache_write_input_tokens = ?, answer_output_tokens = ?,
                        answer_reasoning_output_tokens = ?, answer_total_tokens = ?,
                        usage_recorded_at = ?
                    WHERE id = ?
                    """,
                    (
                        usage.get("input_tokens", 0),
                        usage.get("cached_input_tokens", 0),
                        usage.get("cache_write_input_tokens", 0),
                        usage.get("output_tokens", 0),
                        usage.get("reasoning_output_tokens", 0),
                        usage.get("total_tokens", 0),
                        completed_at.isoformat(),
                        row["id"],
                    ),
                )
            return cursor.rowcount == 1

    def record_usage(
        self, session_id: str, turn_id: str, model: str, usage: dict[str, int]
    ) -> bool:
        """Backward-compatible wrapper for callers that have an answer usage receipt."""
        return self.record_completion(session_id, turn_id, model, usage)

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

    def rows_since(
        self, since: datetime | None = None, session_id: str | None = None
    ) -> list[sqlite3.Row]:
        """Return audit rows in chronological order for local reporting.

        Analytics deliberately queries only route metadata and token counters. Prompt text is not
        selected or emitted by the reporting commands.
        """
        clauses: list[str] = []
        params: list[object] = []
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since.astimezone(timezone.utc).isoformat())
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        query = "SELECT * FROM routing_decisions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC, id ASC"
        with self.connection() as connection:
            return list(connection.execute(query, tuple(params)).fetchall())

    def previous_tier(self, session_id: str) -> Tier | None:
        row = self.latest(session_id)
        return Tier.parse(row["selected_tier"]) if row else None

    def route_preference(
        self, session_id: str, route_scope: str = "root", agent_id: str | None = None
    ) -> str | None:
        """Return the active explicit backend preference for this routing scope."""
        predicate = "agent_id IS NULL" if agent_id is None else "agent_id = ?"
        params: tuple[object, ...] = (session_id, route_scope)
        if agent_id is not None:
            params += (agent_id,)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT sticky_backend FROM routing_decisions "
                f"WHERE session_id = ? AND route_scope = ? AND {predicate} "
                "ORDER BY id DESC LIMIT 1",
                params,
            ).fetchone()
        return str(row["sticky_backend"]) if row and row["sticky_backend"] else None

    def provider_state_is_mixed(
        self, session_id: str, route_scope: str = "root", agent_id: str | None = None
    ) -> bool:
        predicate = "agent_id IS NULL" if agent_id is None else "agent_id = ?"
        params: tuple[object, ...] = (session_id, route_scope)
        if agent_id is not None:
            params += (agent_id,)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT strip_provider_state FROM routing_decisions "
                f"WHERE session_id = ? AND route_scope = ? AND {predicate} "
                "ORDER BY id DESC LIMIT 1",
                params,
            ).fetchone()
        return bool(row and row["strip_provider_state"])

    def label(self, decision_id: int, outcome: str, notes: str | None = None) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE routing_decisions SET outcome_label = ?, outcome_notes = ? WHERE id = ?",
                (outcome, notes, decision_id),
            )
            return cursor.rowcount == 1
