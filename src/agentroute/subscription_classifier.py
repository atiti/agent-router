"""Bounded classifier calls through Codex's existing authenticated providers."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from .classifier import CLASSIFIER_SYSTEM_PROMPT, ClassifierResult, _task_context
from .config import AppConfig, codex_home
from .models import RouteContext
from .profiles import account_credential_home, routed_codex_binary
from .providers import _provider_block, credentials_path


class SubscriptionClassifier:
    source = "subscription_llm"

    def __init__(self, app_config: AppConfig):
        self.app_config = app_config
        self.config = app_config.routing.classifier
        self.last_status = "idle"
        self.last_error_type = None
        self.last_response_metadata = {}
        self.last_usage = {}
        self.last_latency_ms = None
        self.last_request_hash = None
        self.last_previous_context_chars = 0
        self.last_objective_context_chars = 0

    def classify(self, context: RouteContext) -> ClassifierResult:
        self.last_status = "started"
        self.last_error_type = None
        self.last_response_metadata = {}
        self.last_usage = {}
        self.last_latency_ms = None
        previous, objective, state = _task_context(context, self.config)
        self.last_previous_context_chars = len(previous)
        self.last_objective_context_chars = len(objective)
        payload = json.dumps(
            dict(
                latest_user_message=context.latest_prompt,
                previous_assistant_context=previous,
                ongoing_objective=objective,
                task_state=state,
                current_tier=str(context.current_tier),
            ),
            sort_keys=True,
        )
        prompt = CLASSIFIER_SYSTEM_PROMPT + "\nClassify only. Do not execute tools.\n" + payload
        self.last_request_hash = hashlib.sha256(prompt.encode()).hexdigest()
        started = time.perf_counter()
        try:
            return self._classify(prompt)
        except Exception as error:
            self.last_status = (
                "timeout" if isinstance(error, subprocess.TimeoutExpired) else "error"
            )
            self.last_error_type = (
                "timeout" if self.last_status == "timeout" else type(error).__name__
            )
            raise
        finally:
            self.last_latency_ms = round((time.perf_counter() - started) * 1000, 1)

    def _classify(self, prompt: str) -> ClassifierResult:
        config = self.config
        backend = self.app_config.backends.get(config.backend)
        if config.backend not in self.app_config.routing.provider_selection.subscription_backends:
            raise ValueError("classifier backend must be subscription-backed")
        if backend is None or not backend.enabled:
            raise ValueError("classifier backend is absent or disabled")
        auth_home = codex_home()
        if config.backend == "gpt":
            name = config.chatgpt_profile or self.app_config.capacity.active_profile
            if config.chatgpt_profile and name not in self.app_config.capacity.profiles:
                raise ValueError("unknown classifier ChatGPT profile")
            profile = self.app_config.capacity.profiles.get(name)
            if profile is not None:
                auth_home = account_credential_home(name, profile)
            auth = auth_home / "auth.json"
            # Fail closed: subscription mode must never silently use an API key.
            if not auth.is_file() or json.loads(auth.read_text()).get("auth_mode") != "chatgpt":
                raise ValueError("classifier requires a logged-in ChatGPT subscription profile")
        with tempfile.TemporaryDirectory(prefix="agentroute-classifier-") as temporary:
            isolated = Path(temporary)
            if config.backend == "gpt":
                (isolated / "auth.json").symlink_to(auth.resolve())
            # No user config, instructions, hooks, MCP servers, history, goals or shared owner.
            (isolated / "config.toml").write_text(
                _provider_block(self.app_config, preserve_reasoning_compatibility=True)
            )
            schema = isolated / "schema.json"
            schema.write_text(
                json.dumps(
                    {
                        "type": "object",
                        "properties": {
                            "tier": {"type": "string", "enum": ["FAST", "NORMAL", "SMART", "MAX"]},
                            "reasoning_effort": {
                                "type": "string",
                                "enum": ["low", "medium", "high", "xhigh"],
                            },
                            "confidence": {"type": "number"},
                            "task_type": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": [
                            "tier",
                            "reasoning_effort",
                            "confidence",
                            "task_type",
                            "reason",
                        ],
                        "additionalProperties": False,
                    }
                )
            )
            output = isolated / "result.json"
            command = [
                str(config.codex_binary or routed_codex_binary()),
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--ignore-rules",
                "--disable",
                "hooks",
                "--disable",
                "shell_tool",
                "-c",
                "instructions=" + json.dumps(CLASSIFIER_SYSTEM_PROMPT),
                "-c",
                "developer_instructions=" + json.dumps("Classify only; never call tools."),
                "-c",
                "features.skills=false",
                "-c",
                "include_permissions_instructions=false",
                "-c",
                "include_apps_instructions=false",
                "-c",
                "include_collaboration_mode_instructions=false",
                "-c",
                "features.apps=false",
                "-c",
                "model_provider=" + json.dumps(backend.codex_provider),
                "-c",
                "model_reasoning_effort=" + json.dumps(config.reasoning_effort),
                "-c",
                'forced_login_method="chatgpt"'
                if config.backend == "gpt"
                else 'model_reasoning_summary="none"',
                "-m",
                config.model,
                "-s",
                "read-only",
                "-C",
                temporary,
                "--output-schema",
                str(schema),
                "-o",
                str(output),
                "--json",
                "-",
            ]
            environment = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("CODEX_", "AGENTROUTE_", "HC_"))
                and k not in {"OPENAI_API_KEY", "OPENAI_BASE_URL"}
            }
            if config.backend != "gpt" and backend.api_key_env:
                key = backend.api_key_env
                value = os.environ.get(key)
                if not value and credentials_path().is_file():
                    for line in credentials_path().read_text().splitlines():
                        parts = shlex.split(line)
                        if len(parts) == 2 and parts[0] == "export":
                            name, separator, stored = parts[1].partition("=")
                            if separator and name == key:
                                value = stored
                if not value:
                    raise ValueError("selected classifier backend credential is unavailable")
                environment[key] = value
            environment["CODEX_HOME"] = temporary
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                start_new_session=True,
            )
            try:
                stdout, _ = process.communicate(prompt, timeout=config.timeout_seconds)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise
            if process.returncode or not output.is_file():
                raise RuntimeError("subscription classifier failed without a valid result")
            for line in stdout.splitlines():
                event = json.loads(line)
                item = event.get("item") or {}
                if item.get("type") in {
                    "command_execution",
                    "file_change",
                    "mcp_tool_call",
                    "web_search",
                    "collab_tool_call",
                }:
                    raise RuntimeError("classifier attempted tool activity")
                if event.get("type") == "turn.completed":
                    self.last_usage = event.get("usage") or {}
            result = ClassifierResult.from_payload(json.loads(output.read_text()))
            self.last_response_metadata = {
                "requested_model": config.model,
                "backend": config.backend,
                "transport": "codex_subscription",
            }
            self.last_status = "succeeded"
            return result
