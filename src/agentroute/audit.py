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
    proposed_tier TEXT,
    comparison_tier TEXT,
    task_context_used INTEGER NOT NULL DEFAULT 0,
    risk_floor_applied INTEGER NOT NULL DEFAULT 0,
    outcome_label TEXT,
    outcome_notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_routing_session
ON routing_decisions(session_id, id DESC);
"""

MIGRATIONS = {
    "classifier_version": "TEXT NOT NULL DEFAULT 'legacy'",
    "proposed_tier": "TEXT",
    "comparison_tier": "TEXT",
    "task_context_used": "INTEGER NOT NULL DEFAULT 0",
    "risk_floor_applied": "INTEGER NOT NULL DEFAULT 0",
    "outcome_label": "TEXT",
    "outcome_notes": "TEXT",
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
                    created_at, session_id, provider, current_tier, selected_tier,
                    model, reasoning_effort, confidence, raw_score, reason_codes,
                    contributions, prompt_hash, prompt, manual_override, inherited, switched,
                    classifier_version, proposed_tier, comparison_tier, task_context_used,
                    risk_floor_applied
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    decision.session_id,
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
                    int(decision.risk_floor_applied),
                ),
            )
            return int(cursor.lastrowid)

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
