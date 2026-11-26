"""Antigravity subscription as a Codex provider, driven through its own CLI.

Antigravity serves Gemini behind an entitlement check tied to Google's own
clients: replaying its wire protocol from here returns
``403 SUBSCRIPTION_REQUIRED``, and the OAuth token that owns the subscription
carries ``aicode`` rather than the Gemini API's ``generative-language`` scope.
The supported route to that subscription is the ``agy`` command itself.

So this bridge runs ``agy`` per turn using its documented structured interface
(``--output-format stream-json``) and translates the result into Responses
events. That buys three things a plain text wrapper cannot:

* incremental assistant text, because agy streams ``text_delta``;
* real token accounting, because agy reports its own usage per step and turn;
* conversation continuity, because agy can resume a conversation by ID.

Antigravity executes its own tools inside that turn. Codex therefore sees a
text-only model here: handing it tools as well would let two agents act on the
same workspace without a single approval path. ``--mode``, the working
directory and the tool surface are configured on the agy side.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

CONTEXT_WINDOW = 1_000_000
DEFAULT_BINARY = "agy"
RESPONSES_PATH = "/v1/responses"

MODELS: dict[str, str] = {
    "fast": "gemini-3.8-flash-low",
    "normal": "gemini-3.8-flash-medium",
    "smart": "gemini-3.8-flash-high",
    "max": "gemini-4-hydrogen-high",
}
TIER_EFFORT: dict[str, str] = {"fast": "low", "normal": "medium", "smart": "high", "max": "xhigh"}


def binary() -> str:
    return os.environ.get("AGENTROUTE_ANTIGRAVITY_BINARY") or DEFAULT_BINARY


def workspace() -> str:
    """Where agy acts. Codex does not send its cwd, so this is configured."""
    return os.environ.get("AGENTROUTE_ANTIGRAVITY_CWD") or str(Path.home() / "workspace")


def log(message: str) -> None:
    """Operational log line. Never includes prompts, output, or credentials."""
    print(f"agentroute-antigravity: {message}", flush=True)


class AntigravityError(RuntimeError):
    """agy is missing, unauthenticated, or refused the turn."""


def sessions_path() -> Path:
    home = Path(os.environ.get("AGENTROUTE_HOME") or (Path.home() / ".agentroute"))
    return home / "antigravity-sessions.json"


class Sessions:
    """Map a Codex thread onto an agy conversation so turns keep their context."""

    def __init__(self, path: Path | None = None, limit: int = 400) -> None:
        self.path = path or sessions_path()
        self.limit = limit
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self._data: dict[str, str] = data if isinstance(data, dict) else {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def put(self, key: str, conversation: str) -> None:
        self._data[key] = conversation
        if len(self._data) > self.limit:
            for stale in list(self._data)[: len(self._data) - self.limit]:
                self._data.pop(stale, None)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass


def thread_key(body: dict[str, Any]) -> str:
    """A stable identity for the caller's conversation."""
    explicit = body.get("prompt_cache_key")
    if isinstance(explicit, str) and explicit:
        return f"cache:{explicit}"
    for item in body.get("input") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "") != "user":
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "input_text":
                text = str(block.get("text") or "")
                if text:
                    return "first:" + hashlib.sha256(text.encode()).hexdigest()[:32]
    return "anonymous"


def latest_prompt(body: dict[str, Any]) -> str:
    """The newest user text, which is the turn agy has to answer."""
    for item in reversed(body.get("input") or []):
        if not isinstance(item, dict) or str(item.get("role") or "") != "user":
            continue
        parts = [
            str(block.get("text"))
            for block in item.get("content") or []
            if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"]
        ]
        if parts:
            return "\n".join(parts)
    if isinstance(body.get("input"), str):
        return body["input"]
    raise AntigravityError("no user message to send to Antigravity")


def effort_for(model: str, requested: str | None) -> str | None:
    """agy names effort inside the model id; pass it explicitly when it does."""
    suffix = model.rsplit("-", 1)[-1]
    if suffix in {"low", "medium", "high", "xhigh", "max"}:
        return suffix
    return requested if requested in {"low", "medium", "high", "xhigh", "max"} else None


def command(*, prompt: str, model: str, effort: str | None, conversation: str | None) -> list[str]:
    argv = [
        binary(),
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--model",
        model,
    ]
    if effort:
        argv += ["--effort", effort]
    if conversation:
        argv += ["--conversation", conversation]
    mode = os.environ.get("AGENTROUTE_ANTIGRAVITY_MODE")
    if mode:
        argv += ["--mode", mode]
    return argv


def usage_from(payload: Any) -> dict[str, int]:
    """Map agy's token counts onto Responses usage."""
    if not isinstance(payload, dict):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    prompt = int(payload.get("input_tokens") or 0)
    output = int(payload.get("output_tokens") or 0) + int(payload.get("thinking_tokens") or 0)
    total = int(payload.get("total_tokens") or (prompt + output))
    cached = int(payload.get("cache_read_tokens") or 0)
    usage = {"input_tokens": prompt, "output_tokens": output, "total_tokens": total}
    if cached:
        usage["cached_input_tokens"] = cached
    return usage


def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class Turn:
    """One agy invocation, translated into Responses events."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.response_id = f"resp_{uuid.uuid4().hex}"
        self.item_id = f"msg_{uuid.uuid4().hex}"
        self.text: list[str] = []
        self.usage = usage_from(None)
        self.conversation: str | None = None
        self.status: str | None = None
        self.tools: list[str] = []
        self.started = False
        self.opened = False

    def _created(self) -> bytes:
        self.started = True
        return sse(
            "response.created",
            {
                "type": "response.created",
                "response": {
                    "id": self.response_id,
                    "object": "response",
                    "status": "in_progress",
                    "model": self.model,
                },
            },
        )

    def _open_text(self) -> Iterator[bytes]:
        if self.opened:
            return
        self.opened = True
        yield sse(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "message",
                    "id": self.item_id,
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": ""}],
                },
            },
        )
        yield sse(
            "response.content_part.added",
            {
                "type": "response.content_part.added",
                "item_id": self.item_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": ""},
            },
        )

    def feed(self, event: dict[str, Any]) -> Iterator[bytes]:
        if not self.started:
            yield self._created()
        kind = event.get("event")
        if kind == "step_update":
            step = event.get("step_update") or {}
            if step.get("step_type") == "agent_response":
                delta = step.get("text_delta")
                if isinstance(delta, str) and delta:
                    yield from self._open_text()
                    self.text.append(delta)
                    yield sse(
                        "response.output_text.delta",
                        {
                            "type": "response.output_text.delta",
                            "item_id": self.item_id,
                            "output_index": 0,
                            "content_index": 0,
                            "delta": delta,
                        },
                    )
                if isinstance(step.get("usage"), dict):
                    self.usage = usage_from(step["usage"])
            elif step.get("step_type") == "tool":
                name = step.get("tool_name")
                if isinstance(name, str) and name and step.get("state") == "ACTIVE":
                    self.tools.append(name)
            return
        if kind == "result":
            result = event.get("result") or {}
            self.conversation = result.get("conversation_id") or self.conversation
            self.status = result.get("status")
            if isinstance(result.get("usage"), dict):
                self.usage = usage_from(result["usage"])
            if not self.text and isinstance(result.get("response"), str):
                self.text.append(result["response"])

    def complete(self) -> Iterator[bytes]:
        if not self.started:
            yield self._created()
        yield from self._open_text()
        text = "".join(self.text)
        item = {
            "type": "message",
            "id": self.item_id,
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
        yield sse(
            "response.output_text.done",
            {
                "type": "response.output_text.done",
                "item_id": self.item_id,
                "output_index": 0,
                "content_index": 0,
                "text": text,
            },
        )
        yield sse(
            "response.output_item.done",
            {"type": "response.output_item.done", "output_index": 0, "item": item},
        )
        yield sse(
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "id": self.response_id,
                    "object": "response",
                    "status": "completed",
                    "model": self.model,
                    "output": [item],
                    "usage": self.usage,
                },
            },
        )


def iter_events(stream: Any) -> Iterator[dict[str, Any]]:
    for raw in stream:
        line = raw.decode(errors="replace").strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def run_turn(
    prompt: str, model: str, effort: str | None, conversation: str | None
) -> subprocess.Popen:
    executable = shutil.which(binary())
    if not executable:
        raise AntigravityError(
            f"Antigravity CLI not found ({binary()}); install it and sign in with `agy`"
        )
    argv = command(prompt=prompt, model=model, effort=effort, conversation=conversation)
    argv[0] = executable
    return subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=workspace(),
        text=False,
    )


class AntigravityHandler(BaseHTTPRequestHandler):
    """Serve Codex's Responses endpoint over the Antigravity CLI."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:
        return

    def _json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path.rstrip("/") in {"/healthz", "/v1/healthz"}:
            from .antigravity_usage import fetch_limits

            resolved = shutil.which(binary())
            limits = fetch_limits(binary=resolved)
            self._json(
                200,
                {
                    "provider": "antigravity",
                    "auth": "antigravity-subscription",
                    "tool_calling": "none",
                    "tool_calling_mode": "agy_native_tools",
                    "context_window": CONTEXT_WINDOW,
                    "models": MODELS,
                    "binary": resolved,
                    "workspace": workspace(),
                    "subscription": limits,
                },
            )
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if not self.path.startswith(RESPONSES_PATH):
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self._json(400, {"error": "invalid JSON body"})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": "invalid JSON body"})
            return
        model = str(body.get("model") or MODELS["normal"])
        sessions = Sessions()
        key = thread_key(body)
        conversation = sessions.get(key)
        reasoning = body.get("reasoning")
        requested = reasoning.get("effort") if isinstance(reasoning, dict) else None
        effort = effort_for(model, str(requested) if requested else None)
        try:
            prompt = latest_prompt(body)
            process = run_turn(prompt, model, effort, conversation)
        except AntigravityError as exc:
            self._json(503, {"error": str(exc)})
            return
        turn = Turn(model)
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for event in iter_events(process.stdout):
                    for frame in turn.feed(event):
                        self.wfile.write(frame)
            except (OSError, ValueError) as exc:
                log(f"stream ended early: {type(exc).__name__}")
            finally:
                process.wait(timeout=30)
            for frame in turn.complete():
                self.wfile.write(frame)
            self.close_connection = True
            self._remember(sessions, key, turn)
            return
        for event in iter_events(process.stdout):
            list(turn.feed(event))
        process.wait(timeout=30)
        list(turn.complete())
        self._remember(sessions, key, turn)
        self._json(
            200,
            {
                "id": turn.response_id,
                "object": "response",
                "status": "completed",
                "model": model,
                "output": [
                    {
                        "type": "message",
                        "id": turn.item_id,
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "".join(turn.text)}],
                    }
                ],
                "usage": turn.usage,
            },
        )

    @staticmethod
    def _remember(sessions: Sessions, key: str, turn: Turn) -> None:
        if turn.conversation:
            sessions.put(key, turn.conversation)


class AntigravityServer(ThreadingHTTPServer):
    daemon_threads = True


def serve(host: str = "127.0.0.1", port: int = 8816) -> None:
    server = AntigravityServer((host, port), AntigravityHandler)
    log(f"listening on http://{host}:{port}/v1 (antigravity subscription via agy)")
    server.serve_forever()
