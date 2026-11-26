"""Local preferences and recent conversation context for AgentRoute Voice."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .config import agentroute_home

OPTIONS = {
    "tone": {"direct", "conversational"},
    "detail": {"brief", "detailed"},
    "updates": {"all", "blockers", "quiet"},
    "cadence": {"frequent", "normal", "occasional"},
}
DEFAULTS = {"tone": "direct", "detail": "brief", "updates": "all", "cadence": "normal"}
CADENCE_SECONDS = {"frequent": 12, "normal": 30, "occasional": 120}
MAX_HISTORY_BYTES = 32_768
MAX_HISTORY_CHARS = 3_000


def preferences_path() -> Path:
    return agentroute_home() / "voice-preferences.json"


def load_preferences() -> dict[str, str]:
    try:
        data = json.loads(preferences_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        key: data[key] if isinstance(data.get(key), str) and data[key] in OPTIONS[key] else value
        for key, value in DEFAULTS.items()
    }


def save_preference(key: str, value: str) -> dict[str, str]:
    if value not in OPTIONS.get(key, set()):
        raise ValueError("Unsupported voice preference. Check the available options.")
    data = load_preferences()
    data[key] = value
    path = preferences_path()
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                            prefix=".voice-preferences-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return data


def recent_dialogue(current: Path) -> list[dict[str, str]]:
    """Read a bounded tail of the most recent local conversations."""
    records = []
    remaining = MAX_HISTORY_CHARS
    for path in sorted(current.parent.glob("*.jsonl"), reverse=True)[:8]:
        if path == current or path.is_symlink():
            continue
        try:
            with path.open("rb") as stream:
                size = stream.seek(0, os.SEEK_END)
                stream.seek(max(0, size - MAX_HISTORY_BYTES))
                if size > MAX_HISTORY_BYTES:
                    stream.readline()
                lines = stream.read(MAX_HISTORY_BYTES).splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                row = json.loads(line)
                role, text = row["role"], row["text"]
            except (ValueError, KeyError, TypeError):
                continue
            if role not in ("user", "assistant") or not isinstance(text, str) or not text.strip():
                continue
            text = text[:min(750, remaining)]
            records.append({"role": role, "text": text})
            remaining -= len(text)
            if not remaining or len(records) >= 16:
                return records[::-1]
    return records[::-1]
