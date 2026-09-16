from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import agentroute_home


def hook_command(event: str = "user-prompt-submit") -> str:
    return str(agentroute_home() / "bin" / "agentroute") + f" hook codex {event}"


def _merge_command(hooks: dict[str, Any], event: str, command: str, status: str) -> None:
    groups = hooks.setdefault(event, [])
    for group in groups:
        for item in group.get("hooks", []):
            if "agentroute" in str(item.get("command", "")):
                item.update({"type": "command", "command": command, "statusMessage": status})
                return
    groups.append(
        {"hooks": [{"type": "command", "command": command, "statusMessage": status}]}
    )


def merge_codex_hook(path: Path | None = None) -> tuple[Path, Path | None]:
    """Merge AgentRoute into hooks.json and preserve any existing configuration."""
    path = path or Path.home() / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if path.exists():
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.agentroute-backup-{timestamp}")
        shutil.copy2(path, backup)
    else:
        payload = {}
    hooks = payload.setdefault("hooks", {})
    _merge_command(
        hooks,
        "UserPromptSubmit",
        hook_command("user-prompt-submit"),
        "AgentRoute is selecting a model",
    )
    _merge_command(hooks, "Stop", hook_command("stop"), "AgentRoute is recording token usage")
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path, backup
