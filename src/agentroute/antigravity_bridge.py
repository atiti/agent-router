"""The Antigravity subscription as a model behind Codex's own harness.

Antigravity serves Gemini only to Google's own clients: its quota and generation
endpoints answer ``403 SUBSCRIPTION_REQUIRED``, and the subscription token
carries ``aicode`` instead of the Gemini API's ``generative-language`` scope. The
licensed route to that subscription is the ``agy`` command.

Delegating a whole turn to ``agy`` would swap harnesses: Codex's instructions,
skills, tools and approvals would be replaced by Antigravity's. That makes two
agents with two approval paths, and it makes Gemini results incomparable with
every other backend.

So ``agy`` is used strictly as a transport for one model turn. It runs in plan
mode, where it executes nothing, and is asked to answer a single question: given
this conversation and these tool definitions, reply with text or with exactly
one tool call. Codex executes the tool and sends the result back, exactly as it
does for the Gemini API and Claude backends. The conversation, skills and tool
authority stay with Codex.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

PROTOCOL = """\
You are the language model inside another agent harness. The harness executes \
tools; you never run anything yourself, and you must not describe running it.

Answer the conversation below by replying with exactly one of:

1. A single JSON object, on one line, to call a tool:
   {"tool":"<tool name>","arguments":{...}}
2. Ordinary prose, when no tool is needed.

Rules:
- Call at most one tool per reply.
- Use only the tools listed in TOOL DEFINITIONS, with their exact names.
- Put arguments in "arguments" as a JSON object matching the schema.
- Never wrap the JSON in code fences and never add words around it.
- Never invent a tool result. If you need a result, call the tool and stop.
"""


def binary() -> str:
    return os.environ.get("AGENTROUTE_ANTIGRAVITY_BINARY") or DEFAULT_BINARY


def workspace() -> str:
    """Where agy starts. Codex does not send its cwd; nothing here executes."""
    return os.environ.get("AGENTROUTE_ANTIGRAVITY_CWD") or str(os.path.expanduser("~"))


def log(message: str) -> None:
    """Operational log line. Never includes prompts, output, or credentials."""
    print(f"agentroute-antigravity: {message}", flush=True)


class AntigravityError(RuntimeError):
    """agy is missing, unauthenticated, or refused the turn."""


def _schema(parameters: Any, depth: int = 0) -> str:
    """Render a JSON schema compactly enough for a prompt."""
    if not isinstance(parameters, dict) or depth > 6:
        return "{}"
    kind = str(parameters.get("type") or "object").lower()
    properties = parameters.get("properties")
    if not isinstance(properties, dict) or not properties:
        return kind
    required = {str(name) for name in parameters.get("required") or []}
    fields = []
    for name, spec in properties.items():
        spec = spec if isinstance(spec, dict) else {}
        rendered = _schema(spec, depth + 1)
        marker = "" if name in required else "?"
        description = str(spec.get("description") or "").replace("\n", " ")[:110]
        fields.append(f"{name}{marker}:{rendered}" + (f" // {description}" if description else ""))
    return "{" + ", ".join(fields) + "}"


def tool_definitions(body: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """Describe the caller's tools, and record which are freeform."""
    lines: list[str] = []
    kinds: dict[str, str] = {}
    for raw in body.get("tools") or []:
        if not isinstance(raw, dict):
            continue
        if raw.get("type") == "custom":
            name = raw.get("name")
            if not isinstance(name, str) or not name:
                continue
            kinds[name] = "custom"
            lines.append(
                f'- {name}: freeform text tool. arguments {{"input": string}} '
                f"// {str(raw.get('description') or '')[:110]}".rstrip()
            )
            continue
        spec = raw.get("function") if isinstance(raw.get("function"), dict) else raw
        name = spec.get("name")
        if not isinstance(name, str) or not name:
            continue
        kinds[name] = "function"
        lines.append(
            f"- {name}: arguments {_schema(spec.get('parameters'))} "
            f"// {str(spec.get('description') or '')[:110]}".rstrip()
        )
    return lines, kinds


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item["text"])
        for item in content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    )


def transcript(body: dict[str, Any]) -> list[str]:
    """Replay the caller's history as plain turns."""
    lines: list[str] = []
    items = body.get("input")
    if isinstance(items, str):
        return [f"USER:\n{items}"]
    call_names: dict[str, str] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in {"function_call", "custom_tool_call"}:
            name = str(item.get("name") or "")
            call_id = str(item.get("call_id") or "")
            if call_id:
                call_names[call_id] = name
            arguments = item.get("arguments", item.get("input"))
            lines.append(f"ASSISTANT CALLED {name} with {arguments}")
            continue
        if kind in {"function_call_output", "custom_tool_call_output"}:
            name = call_names.get(str(item.get("call_id") or ""), "tool")
            output = item.get("output")
            text = output if isinstance(output, str) else _content_text(output)
            lines.append(f"RESULT OF {name}:\n{text}")
            continue
        text = _content_text(item.get("content"))
        if not text and isinstance(item.get("text"), str):
            text = item["text"]
        if not text.strip():
            continue
        role = "ASSISTANT" if str(item.get("role") or "") == "assistant" else "USER"
        lines.append(f"{role}:\n{text}")
    return lines


def build_prompt(body: dict[str, Any]) -> str:
    """One self-contained question for the model, carrying the caller's context."""
    sections: list[str] = [PROTOCOL]
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        sections.append("HARNESS INSTRUCTIONS (authoritative):\n" + instructions.strip())
    lines, _kinds = tool_definitions(body)
    sections.append("TOOL DEFINITIONS:\n" + ("\n".join(lines) if lines else "(none)"))
    turns = transcript(body)
    sections.append("CONVERSATION:\n" + ("\n\n".join(turns) if turns else "USER:\nContinue."))
    sections.append("REPLY:")
    return "\n\n".join(sections)


def parse_reply(text: str) -> tuple[str, str, Any]:
    """Classify a reply as a tool call or prose.

    Models occasionally wrap JSON despite the instruction, so a fenced block and
    a single-object fallback are accepted; anything else is treated as prose.
    """
    stripped = text.strip()
    candidates = [stripped]
    if "```" in stripped:
        for chunk in stripped.split("```"):
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            if chunk.startswith("{"):
                candidates.append(chunk)
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("tool"), str):
            arguments = payload.get("arguments")
            return "tool", payload["tool"], arguments if isinstance(arguments, dict) else {}
    return "text", text, None


def command(prompt: str, model: str, effort: str | None) -> list[str]:
    """agy as a stateless, non-executing model call."""
    argv = [
        binary(),
        "-p",
        prompt,
        "--mode",
        "plan",
        "--output-format",
        "stream-json",
        "--model",
        model,
    ]
    if effort:
        argv += ["--effort", effort]
    return argv


def effort_for(model: str, requested: str | None) -> str | None:
    suffix = model.rsplit("-", 1)[-1]
    if suffix in {"low", "medium", "high", "xhigh", "max"}:
        return suffix
    return requested if requested in {"low", "medium", "high", "xhigh", "max"} else None


def usage_from(payload: Any) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    prompt = int(payload.get("input_tokens") or 0)
    output = int(payload.get("output_tokens") or 0) + int(payload.get("thinking_tokens") or 0)
    total = int(payload.get("total_tokens") or (prompt + output))
    usage = {"input_tokens": prompt, "output_tokens": output, "total_tokens": total}
    cached = int(payload.get("cache_read_tokens") or 0)
    if cached:
        usage["cached_input_tokens"] = cached
    return usage


def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


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


@dataclass
class Reply:
    text: str = ""
    usage: dict[str, int] | None = None


def run_turn(prompt: str, model: str, effort: str | None) -> Reply:
    """Run one plan-mode agy call and collect its reply."""
    executable = shutil.which(binary()) or (binary() if os.path.isabs(binary()) else None)
    if not executable:
        raise AntigravityError(
            f"Antigravity CLI not found ({binary()}); install it and sign in with `agy`"
        )
    argv = command(prompt, model, effort)
    argv[0] = executable
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            timeout=900,
            cwd=workspace(),
        )
    except subprocess.TimeoutExpired as exc:
        raise AntigravityError("Antigravity turn timed out") from exc
    except OSError as exc:
        raise AntigravityError(f"cannot run Antigravity CLI: {exc}") from exc
    reply = Reply()
    for event in iter_events(result.stdout.splitlines(keepends=True)):
        if event.get("event") == "result":
            payload = event.get("result") or {}
            reply.text = str(payload.get("response") or "")
            reply.usage = usage_from(payload.get("usage"))
            if payload.get("status") not in {None, "SUCCESS"}:
                raise AntigravityError(f"Antigravity turn failed: {payload.get('status')}")
    if not reply.text:
        detail = result.stderr.decode(errors="replace").strip()[:300]
        raise AntigravityError(detail or "Antigravity returned no response")
    return reply


class AntigravityHandler(BaseHTTPRequestHandler):
    """Serve Codex's Responses endpoint, with Codex owning tools and history."""

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
            self._json(
                200,
                {
                    "provider": "antigravity",
                    "auth": "antigravity-subscription",
                    "harness": "codex",
                    "tool_calling": "full",
                    "tool_calling_mode": "prompt_json_single_call",
                    "context_window": CONTEXT_WINDOW,
                    "models": MODELS,
                    "binary": resolved,
                    "subscription": fetch_limits(binary=resolved),
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
        reasoning = body.get("reasoning")
        requested = reasoning.get("effort") if isinstance(reasoning, dict) else None
        effort = effort_for(model, str(requested) if requested else None)
        _lines, kinds = tool_definitions(body)
        try:
            reply = run_turn(build_prompt(body), model, effort)
        except AntigravityError as exc:
            self._json(503, {"error": str(exc)})
            return
        kind, value, arguments = parse_reply(reply.text)
        item = self._item(kind, value, arguments, kinds)
        response = {
            "id": f"resp_{uuid.uuid4().hex}",
            "object": "response",
            "status": "completed",
            "model": model,
            "output": [item],
            "usage": reply.usage or usage_from(None),
        }
        if not body.get("stream"):
            self._json(200, response)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        for frame in self._stream(item, response):
            self.wfile.write(frame)
        self.close_connection = True

    @staticmethod
    def _item(kind: str, value: str, arguments: Any, kinds: dict[str, str]) -> dict[str, Any]:
        if kind != "tool":
            return {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex}",
                "role": "assistant",
                "content": [{"type": "output_text", "text": value}],
            }
        call_id = f"call_{uuid.uuid4().hex}"
        if kinds.get(value) == "custom":
            raw = arguments.get("input") if isinstance(arguments, dict) else None
            return {
                "type": "custom_tool_call",
                "id": f"ctc_{uuid.uuid4().hex}",
                "status": "completed",
                "call_id": call_id,
                "name": value,
                "input": raw if isinstance(raw, str) else json.dumps(arguments),
            }
        return {
            "type": "function_call",
            "id": f"fc_{uuid.uuid4().hex}",
            "status": "completed",
            "call_id": call_id,
            "name": value,
            "arguments": json.dumps(arguments or {}, separators=(",", ":")),
        }

    def _stream(self, item: dict[str, Any], response: dict[str, Any]) -> Iterator[bytes]:
        # The completed event must carry the same item that was just emitted.
        response = {**response, "output": [item]}
        yield sse(
            "response.created",
            {
                "type": "response.created",
                "response": {
                    "id": response["id"],
                    "object": "response",
                    "status": "in_progress",
                    "model": response["model"],
                },
            },
        )
        yield sse(
            "response.output_item.added",
            {"type": "response.output_item.added", "output_index": 0, "item": item},
        )
        if item["type"] == "message":
            text = item["content"][0]["text"]
            if text:
                yield sse(
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "item_id": item["id"],
                        "output_index": 0,
                        "content_index": 0,
                        "delta": text,
                    },
                )
            yield sse(
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "item_id": item["id"],
                    "output_index": 0,
                    "content_index": 0,
                    "text": text,
                },
            )
        elif item["type"] == "function_call":
            yield sse(
                "response.function_call_arguments.done",
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": item["id"],
                    "output_index": 0,
                    "arguments": item["arguments"],
                },
            )
        else:
            yield sse(
                "response.custom_tool_call_input.done",
                {
                    "type": "response.custom_tool_call_input.done",
                    "item_id": item["id"],
                    "call_id": item["call_id"],
                    "output_index": 0,
                    "input": item["input"],
                },
            )
        yield sse(
            "response.output_item.done",
            {"type": "response.output_item.done", "output_index": 0, "item": item},
        )
        yield sse("response.completed", {"type": "response.completed", "response": response})


class AntigravityServer(ThreadingHTTPServer):
    daemon_threads = True


def serve(host: str = "127.0.0.1", port: int = 8816) -> None:
    server = AntigravityServer((host, port), AntigravityHandler)
    log(f"listening on http://{host}:{port}/v1 (antigravity model, codex harness)")
    server.serve_forever()
