from __future__ import annotations

import json
import select
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import agentroute_home


def hook_command(event: str = "user-prompt-submit") -> str:
    return str(agentroute_home() / "bin" / "agentroute") + f" hook codex {event}"


class _CodexAppServer:
    """Minimal JSON-RPC client for scoped hook discovery and trust writes."""

    def __init__(self, codex_binary: Path, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self.next_request_id = 1
        self.process = subprocess.Popen(
            [str(codex_binary), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "agentroute",
                    "version": "hook-trust",
                }
            },
        )
        self.notify("notifications/initialized", {})

    def _send(self, payload: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("Codex app-server stdin is unavailable")
        try:
            self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except BrokenPipeError as error:
            detail = ""
            return_code = self.process.poll()
            if self.process.stderr is not None and return_code is not None:
                detail = self.process.stderr.read().strip()
            suffix = f"; stderr: {detail}" if detail else ""
            raise RuntimeError(
                f"Codex app-server exited before receiving {payload.get('method', 'request')}"
                f" (exit code {return_code}){suffix}"
            ) from error

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_request_id
        self.next_request_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        if self.process.stdout is None:
            raise RuntimeError("Codex app-server stdout is unavailable")
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"Codex app-server timed out during {method}")
            readable, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not readable:
                raise RuntimeError(f"Codex app-server timed out during {method}")
            line = self.process.stdout.readline()
            if not line:
                stderr = ""
                if self.process.stderr is not None:
                    stderr = self.process.stderr.read().strip()
                detail = f": {stderr}" if stderr else ""
                raise RuntimeError(f"Codex app-server exited during {method}{detail}")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(
                    f"Codex app-server returned invalid JSON during {method}"
                ) from error
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError(f"Codex app-server {method} failed: {response['error']}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"Codex app-server {method} returned no result")
            return result

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)

    def __enter__(self) -> _CodexAppServer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


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
    _merge_command(
        hooks,
        "SubagentStop",
        hook_command("stop"),
        "AgentRoute is recording subagent token usage",
    )
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path, backup


def trust_agentroute_hooks(
    codex_binary: Path,
    *,
    hooks_path: Path | None = None,
    cwd: Path | None = None,
) -> int:
    """Trust only the exact AgentRoute hooks using hashes computed by Codex itself."""
    hooks_path = (hooks_path or Path.home() / ".codex" / "hooks.json").resolve()
    cwd = (cwd or Path.cwd()).resolve()
    expected_hooks = {
        "userPromptSubmit": hook_command("user-prompt-submit"),
        "stop": hook_command("stop"),
        "subagentStop": hook_command("stop"),
    }
    with _CodexAppServer(codex_binary) as client:
        response = client.request("hooks/list", {"cwds": [str(cwd)]})
        entries = response.get("data", [])
        hooks = entries[0].get("hooks", []) if entries else []
        selected = [
            hook
            for hook in hooks
            if hook.get("handlerType") == "command"
            and hook.get("eventName") in expected_hooks
            and hook.get("command") == expected_hooks[hook["eventName"]]
            and Path(str(hook.get("sourcePath", ""))).resolve() == hooks_path
        ]
        found_events = {str(hook["eventName"]) for hook in selected}
        missing = sorted(expected_hooks.keys() - found_events)
        if missing:
            raise RuntimeError(
                "Codex did not discover the installed AgentRoute hook events: "
                + ", ".join(missing)
            )
        if len(selected) != len(expected_hooks):
            raise RuntimeError("Codex discovered duplicate AgentRoute hook entries")
        trust_state = {
            str(hook["key"]): {"trusted_hash": str(hook["currentHash"])}
            for hook in selected
        }
        client.request(
            "config/batchWrite",
            {
                "edits": [
                    {
                        "keyPath": "hooks.state",
                        "value": trust_state,
                        "mergeStrategy": "upsert",
                    }
                ],
                "filePath": None,
                "expectedVersion": None,
                "reloadUserConfig": True,
            },
        )
    return len(trust_state)
