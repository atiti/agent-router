"""Durable private storage for signed thinking that cannot fit in a replay item."""

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .config import agentroute_home

MAX_BLOCK_BYTES = 4 * 1024 * 1024
MAX_STORE_BYTES = 128 * 1024 * 1024


class ThinkingReplayError(ValueError):
    """A signed block cannot be preserved or replayed without losing data."""


def thinking_store_path() -> Path:
    return agentroute_home() / "state" / "claude-thinking" / "replay.sqlite3"


def store_thinking(raw: bytes) -> str:
    """Persist an exact envelope before exposing its content-addressed reference.

    Never evict existing references: they may belong to a task resumed later.
    Refuse new data at the hard capacity limit instead of breaking those tasks.
    """
    if len(raw) > MAX_BLOCK_BYTES:
        raise ThinkingReplayError("Claude thinking exceeds the 4 MiB replay storage limit.")
    digest = hashlib.sha256(raw).hexdigest()
    path = thinking_store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        if path.is_symlink():
            raise OSError("replay store must be a regular file")
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        with sqlite3.connect(path, timeout=5) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("CREATE TABLE IF NOT EXISTS blocks (digest TEXT PRIMARY KEY, data BLOB)")
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM blocks WHERE digest = ?", (digest,)).fetchone():
                return digest
            size = db.execute("SELECT COALESCE(SUM(length(data)), 0) FROM blocks").fetchone()[0]
            if size + len(raw) > MAX_STORE_BYTES:
                raise ThinkingReplayError(
                    "Claude thinking replay storage is full (128 MiB); "
                    "preserve it for existing tasks and use lower reasoning effort."
                )
            db.execute("INSERT INTO blocks VALUES (?, ?)", (digest, raw))
    except (OSError, sqlite3.Error) as exc:
        raise ThinkingReplayError("Claude thinking replay storage is unavailable.") from exc
    return digest


def load_thinking(digest: str) -> dict[str, Any]:
    """Read a bounded envelope, checking integrity before parsing any content."""
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ThinkingReplayError("Claude thinking replay reference is invalid.")
    path = thinking_store_path()
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as db:
            row = db.execute(
                "SELECT data FROM blocks WHERE digest = ? AND length(data) <= ?",
                (digest, MAX_BLOCK_BYTES),
            ).fetchone()
    except (OSError, sqlite3.Error) as exc:
        raise ThinkingReplayError(
            "Claude thinking replay storage is unavailable; restore the local replay store."
        ) from exc
    if row is None:
        raise ThinkingReplayError(
            "Claude thinking replay block is missing; restore the replay store."
        )
    raw = row[0]
    if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != digest:
        raise ThinkingReplayError("Claude thinking replay block failed its integrity check.")
    try:
        envelope = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ThinkingReplayError("Claude thinking replay block is invalid.") from exc
    if not isinstance(envelope, dict):
        raise ThinkingReplayError("Claude thinking replay envelope is invalid.")
    return envelope
