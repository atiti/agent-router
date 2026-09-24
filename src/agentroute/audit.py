from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import data_dir
from .models import RouteDecision, Tier


def _bounded_string(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:limit] or None


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
    classifier_reasoning_effort TEXT,
    reasoning_effort_source TEXT NOT NULL DEFAULT 'tier_default',
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
    next_manual_override_tier TEXT,
    next_manual_override_at TEXT,
    answer_model TEXT,
    answer_backend TEXT,
    answer_input_tokens INTEGER,
    answer_cached_input_tokens INTEGER,
    answer_cache_write_input_tokens INTEGER,
    answer_output_tokens INTEGER,
    answer_reasoning_output_tokens INTEGER,
    answer_total_tokens INTEGER,
    usage_recorded_at TEXT,
    reported_answer_model TEXT,
    answer_model_mismatch INTEGER NOT NULL DEFAULT 0,
    route_application_state TEXT NOT NULL DEFAULT 'unknown',
    route_application_reason TEXT,
    actual_backend TEXT,
    actual_model_provider TEXT,
    actual_model TEXT,
    actual_reasoning_effort TEXT,
    turn_completed_at TEXT,
    turn_duration_ms REAL,
    turn_outcome TEXT NOT NULL DEFAULT 'pending',
    completion_source TEXT,
    usage_status TEXT NOT NULL DEFAULT 'pending'
    ,capacity_status TEXT NOT NULL DEFAULT 'disabled'
    ,capacity_detail TEXT
    ,capacity_trigger TEXT
    ,capacity_requested_backend TEXT
    ,capacity_account_id TEXT
    ,capacity_blocked INTEGER NOT NULL DEFAULT 0
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
    "classifier_reasoning_effort": "TEXT",
    "reasoning_effort_source": "TEXT NOT NULL DEFAULT 'tier_default'",
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
    "next_manual_override_tier": "TEXT",
    "next_manual_override_at": "TEXT",
    "answer_model": "TEXT",
    "answer_backend": "TEXT",
    "answer_input_tokens": "INTEGER",
    "answer_cached_input_tokens": "INTEGER",
    "answer_cache_write_input_tokens": "INTEGER",
    "answer_output_tokens": "INTEGER",
    "answer_reasoning_output_tokens": "INTEGER",
    "answer_total_tokens": "INTEGER",
    "usage_recorded_at": "TEXT",
    "reported_answer_model": "TEXT",
    "answer_model_mismatch": "INTEGER NOT NULL DEFAULT 0",
    "route_application_state": "TEXT NOT NULL DEFAULT 'unknown'",
    "route_application_reason": "TEXT",
    "actual_backend": "TEXT",
    "actual_model_provider": "TEXT",
    "actual_model": "TEXT",
    "actual_reasoning_effort": "TEXT",
    "turn_completed_at": "TEXT",
    "turn_duration_ms": "REAL",
    "turn_outcome": "TEXT NOT NULL DEFAULT 'pending'",
    "completion_source": "TEXT",
    "usage_status": "TEXT NOT NULL DEFAULT 'pending'",
    "capacity_status": "TEXT NOT NULL DEFAULT 'disabled'",
    "capacity_detail": "TEXT",
    "capacity_trigger": "TEXT",
    "capacity_requested_backend": "TEXT",
    "capacity_account_id": "TEXT",
    "capacity_blocked": "INTEGER NOT NULL DEFAULT 0",
}


class AuditStore:
    def __init__(self, path: Path | None = None, *, timeout: float = 5.0) -> None:
        self.path = path or data_dir() / "audit.db"
        self.timeout = timeout
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
            # Older releases treated a next-turn manual tier choice as proof that the
            # previous route was wrong. Preserve it as a behavioral signal instead.
            connection.execute(
                "UPDATE routing_decisions SET "
                "next_manual_override_tier = substr(outcome_notes, 29), "
                "next_manual_override_at = COALESCE(next_manual_override_at, created_at), "
                "outcome_label = NULL "
                "WHERE outcome_label = 'overridden' "
                "AND outcome_notes LIKE 'next turn manually selected %' "
                "AND next_manual_override_tier IS NULL"
            )
            # A newer prompt in the same route proves the older turn is no longer active,
            # but it does not prove an exact completion time. Keep latency unknown instead
            # of inflating it with user idle time.
            connection.execute(
                "UPDATE routing_decisions AS old SET turn_outcome = 'superseded', "
                "completion_source = COALESCE(completion_source, 'next_prompt'), "
                "usage_status = 'missing' WHERE turn_outcome = 'pending' "
                "AND turn_completed_at IS NULL AND EXISTS ("
                "SELECT 1 FROM routing_decisions AS newer WHERE newer.id > old.id "
                "AND newer.session_id = old.session_id "
                "AND newer.route_scope = old.route_scope "
                "AND (newer.agent_id = old.agent_id OR "
                "(newer.agent_id IS NULL AND old.agent_id IS NULL)))"
            )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=self.timeout)
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
        application_state: str = "pending",
    ) -> int:
        with self.connection() as connection:
            route_predicate = "agent_id IS NULL" if decision.agent_id is None else "agent_id = ?"
            route_params: tuple[object, ...] = (decision.session_id, decision.route_scope)
            if decision.agent_id is not None:
                route_params += (decision.agent_id,)
            previous = connection.execute(
                "SELECT * FROM routing_decisions WHERE session_id = ? AND route_scope = ? "
                f"AND {route_predicate} ORDER BY id DESC LIMIT 1",
                route_params,
            ).fetchone()
            if (
                decision.manual_override
                and previous is not None
                and not previous["manual_override"]
            ):
                connection.execute(
                    "UPDATE routing_decisions SET next_manual_override_tier = ?, "
                    "next_manual_override_at = ? WHERE id = ?",
                    (str(decision.tier), datetime.now(timezone.utc).isoformat(), previous["id"]),
                )
            if previous is not None and previous["turn_outcome"] == "pending":
                connection.execute(
                    "UPDATE routing_decisions SET turn_outcome = 'superseded', "
                    "completion_source = 'next_prompt', usage_status = 'missing' WHERE id = ?",
                    (previous["id"],),
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
                    classifier_reasoning_effort, reasoning_effort_source,
                    classifier_reason_hash, selection_receipt, selection_receipt_hash
                    ,capacity_status, capacity_detail, capacity_trigger,
                    capacity_requested_backend, capacity_account_id, capacity_blocked
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
                    decision.classifier_reasoning_effort,
                    decision.reasoning_effort_source,
                    decision.classifier_reason_hash,
                    json.dumps(decision.selection_receipt, sort_keys=True, separators=(",", ":")),
                    decision.selection_receipt_hash,
                    decision.capacity_status,
                    decision.capacity_detail,
                    decision.capacity_trigger,
                    decision.capacity_requested_backend,
                    decision.capacity_account_id,
                    int(decision.capacity_blocked),
                ),
            )
            decision_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE routing_decisions SET route_application_state = ? WHERE id = ?",
                (application_state, decision_id),
            )
            return decision_id

    def record_interruption(
        self,
        session_id: str,
        turn_id: str,
        route_scope: str = "root",
        agent_id: str | None = None,
    ) -> bool:
        """Mark one exact turn interrupted from Codex's Interrupt hook."""
        interrupted_at = datetime.now(timezone.utc)
        agent_clause = "agent_id IS NULL" if agent_id is None else "agent_id = ?"
        params: list[object] = [session_id, turn_id, route_scope]
        if agent_id is not None:
            params.append(agent_id)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id, created_at, turn_outcome, turn_completed_at, turn_duration_ms "
                "FROM routing_decisions WHERE session_id = ? AND turn_id = ? "
                f"AND route_scope = ? AND {agent_clause} ORDER BY id DESC LIMIT 1",
                tuple(params),
            ).fetchone()
            if row is None or row["turn_outcome"] in {"completed", "failed"}:
                return False
            if row["turn_duration_ms"] is not None:
                duration_ms = float(row["turn_duration_ms"])
            else:
                started_at = datetime.fromisoformat(str(row["created_at"]))
                duration_ms = max(
                    0.0,
                    (interrupted_at - started_at.astimezone(timezone.utc)).total_seconds()
                    * 1000,
                )
            cursor = connection.execute(
                "UPDATE routing_decisions SET turn_completed_at = COALESCE(turn_completed_at, ?), "
                "turn_duration_ms = COALESCE(turn_duration_ms, ?), turn_outcome = 'interrupted', "
                "completion_source = 'interrupt_hook', "
                "usage_status = CASE WHEN usage_status = 'pending' THEN 'missing' "
                "ELSE usage_status END WHERE id = ?",
                (interrupted_at.isoformat(), duration_ms, row["id"]),
            )
            return cursor.rowcount == 1

    def record_completion(
        self,
        session_id: str,
        turn_id: str,
        model: str,
        usage: dict[str, int] | None = None,
        outcome: str = "completed",
        completion_source: str = "stop_hook",
        route_scope: str | None = None,
        agent_id: str | None = None,
        application_receipt: dict[str, object] | None = None,
        actual_backend: str | None = None,
    ) -> bool:
        """Record first completion time and optional usage for one exact routed turn."""
        completed_at = datetime.now(timezone.utc)
        scope_clause = ""
        params: list[object] = [session_id, turn_id]
        if route_scope is not None:
            scope_clause += " AND route_scope = ?"
            params.append(route_scope)
        if agent_id is not None:
            scope_clause += " AND agent_id = ?"
            params.append(agent_id)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id, created_at, model, model_provider, reasoning_effort, "
                "turn_completed_at, turn_duration_ms, turn_outcome, completion_source, "
                "route_application_state, route_application_reason, actual_backend, "
                "actual_model_provider, actual_model, actual_reasoning_effort "
                "FROM routing_decisions WHERE session_id = ? AND turn_id = ? "
                f"{scope_clause} ORDER BY id DESC LIMIT 1",
                tuple(params),
            ).fetchone()
            if row is None:
                return False
            routed_model = str(row["model"])
            routed_provider = str(row["model_provider"])
            routed_effort = str(row["reasoning_effort"] or "")
            reported_model = model or None
            state, reason, actual_model, actual_provider, actual_effort = self._route_application(
                application_receipt, routed_model, routed_provider, routed_effort
            )
            if application_receipt is None:
                state = str(row["route_application_state"])
                if state == "pending":
                    state = "unknown"
                reason = row["route_application_reason"]
                actual_backend = row["actual_backend"] or actual_backend
                actual_provider = row["actual_model_provider"]
                actual_model = row["actual_model"]
                actual_effort = row["actual_reasoning_effort"]
            receipt_model_observed = actual_model is not None and actual_provider is not None
            answer_model = actual_model if receipt_model_observed else routed_model
            answer_backend = actual_backend if receipt_model_observed else None
            mismatch = bool(
                (reported_model and reported_model != routed_model)
                or (
                    receipt_model_observed
                    and (actual_model != routed_model or actual_provider != routed_provider)
                )
            )
            first_completed_at = row["turn_completed_at"] or completed_at.isoformat()
            if row["turn_duration_ms"] is not None:
                duration_ms = float(row["turn_duration_ms"])
            else:
                started_at = datetime.fromisoformat(str(row["created_at"]))
                duration_ms = max(
                    0.0,
                    (completed_at - started_at.astimezone(timezone.utc)).total_seconds() * 1000,
                )
            interrupted_outcome = (
                row["turn_outcome"] == "interrupted"
                and row["completion_source"] == "interrupt_hook"
            )
            stored_outcome = "interrupted" if interrupted_outcome else outcome
            stored_completion_source = (
                "interrupt_hook" if interrupted_outcome else completion_source
            )
            cursor = connection.execute(
                """
                UPDATE routing_decisions SET
                    answer_model = ?, answer_backend = ?,
                    reported_answer_model = ?, answer_model_mismatch = ?,
                    route_application_state = ?, route_application_reason = ?,
                    actual_backend = ?, actual_model_provider = ?, actual_model = ?,
                    actual_reasoning_effort = ?,
                    turn_completed_at = ?, turn_duration_ms = ?,
                    turn_outcome = ?, completion_source = ?, usage_status = ?
                WHERE id = ?
                """,
                (
                    answer_model,
                    answer_backend,
                    reported_model,
                    int(mismatch),
                    state,
                    reason,
                    answer_backend,
                    actual_provider,
                    actual_model,
                    actual_effort,
                    first_completed_at,
                    duration_ms,
                    stored_outcome,
                    stored_completion_source,
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

    @staticmethod
    def _route_application(
        receipt: dict[str, object] | None,
        requested_model: str,
        requested_provider: str,
        requested_effort: str,
    ) -> tuple[str, str | None, str | None, str | None, str | None]:
        """Validate Codex's turn-scoped report against the exact stored route request."""
        if not isinstance(receipt, dict):
            return "unknown", None, None, None, None

        requested = receipt.get("requested")
        actual = receipt.get("actual")
        requested = requested if isinstance(requested, dict) else {}
        actual = actual if isinstance(actual, dict) else {}
        reported_model = _bounded_string(actual.get("model"), 256)
        reported_provider = _bounded_string(actual.get("provider"), 128)
        reported_effort = _bounded_string(actual.get("reasoning_effort"), 64)
        status = str(receipt.get("status", "unknown"))
        reason = _bounded_string(receipt.get("reason"), 512)

        request_matches = (
            requested.get("model") == requested_model
            and requested.get("provider") == requested_provider
            and (
                not requested_effort
                or requested.get("reasoning_effort") == requested_effort
            )
        )
        if not request_matches:
            return (
                "unknown",
                "Codex application receipt did not match the stored route request",
                reported_model,
                reported_provider,
                reported_effort,
            )

        allowed = {"applied", "rejected", "target_unavailable"}
        if status not in allowed:
            return "unknown", reason, reported_model, reported_provider, reported_effort
        if status == "applied":
            matches = (
                reported_model == requested_model
                and reported_provider == requested_provider
                and (
                    not requested_effort
                    or reported_effort == requested_effort
                )
            )
            if not matches:
                return (
                    "mismatch",
                    reason or "Codex applied settings differ from the requested route",
                    reported_model,
                    reported_provider,
                    reported_effort,
                )
        return status, reason, reported_model, reported_provider, reported_effort

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

    def latest_route(
        self, session_id: str, route_scope: str = "root", agent_id: str | None = None
    ) -> sqlite3.Row | None:
        """Return the latest decision for one root or subagent route."""
        return self._latest_for_route(session_id, route_scope, agent_id)

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

    def _latest_for_route(
        self, session_id: str, route_scope: str = "root", agent_id: str | None = None
    ) -> sqlite3.Row | None:
        predicate = "agent_id IS NULL" if agent_id is None else "agent_id = ?"
        params: tuple[object, ...] = (session_id, route_scope)
        if agent_id is not None:
            params += (agent_id,)
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM routing_decisions "
                f"WHERE session_id = ? AND route_scope = ? AND {predicate} "
                "ORDER BY id DESC LIMIT 1",
                params,
            ).fetchone()

    def previous_tier(
        self, session_id: str, route_scope: str = "root", agent_id: str | None = None
    ) -> Tier | None:
        row = self._latest_for_route(session_id, route_scope, agent_id)
        return Tier.parse(row["selected_tier"]) if row else None

    def previous_backend(
        self, session_id: str, route_scope: str = "root", agent_id: str | None = None
    ) -> str | None:
        row = self._latest_for_route(session_id, route_scope, agent_id)
        return str(row["backend"]) if row and row["backend"] else None

    def previous_reasoning_effort(
        self, session_id: str, route_scope: str = "root", agent_id: str | None = None
    ) -> str | None:
        row = self._latest_for_route(session_id, route_scope, agent_id)
        return str(row["reasoning_effort"]) if row and row["reasoning_effort"] else None

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

    def previous_capacity(
        self, session_id: str, route_scope: str = "root", agent_id: str | None = None
    ) -> tuple[str | None, str | None]:
        row = self._latest_for_route(session_id, route_scope, agent_id)
        if not row:
            return None, None
        return (
            str(row["capacity_status"]) if row["capacity_status"] else None,
            str(row["capacity_requested_backend"] or row["backend"])
            if row["capacity_requested_backend"] or row["backend"]
            else None,
        )

    def subscription_profile_affinity(self, session_id: str) -> str | None:
        """Return the most recently selected ChatGPT account for a session.

        The historical method name remains stable for callers and old audit rows.
        """
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT selection_receipt FROM routing_decisions "
                "WHERE session_id = ? ORDER BY id DESC LIMIT 100",
                (session_id,),
            ).fetchall()
        for row in rows:
            try:
                receipt = json.loads(row["selection_receipt"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            account = receipt.get("chatgpt_account") or receipt.get("subscription_profile")
            if isinstance(account, dict) and account.get("name"):
                return str(account["name"])
        return None

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
