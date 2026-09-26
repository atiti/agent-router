"""Serve Claude models to Codex through a loopback Responses API endpoint.

Codex speaks only ``wire_api = "responses"``, while Anthropic exposes the
Messages API.  This module translates between them: Responses requests become
Anthropic Messages requests, and Anthropic's SSE stream becomes Responses SSE
events, including ``apply_patch``-style freeform tools.

Two credential modes are supported:

``api-key``
    ``ANTHROPIC_API_KEY`` (or the stored backend credential file). This is the
    supported, documented way to call the Anthropic API.

``claude-code``
    The Claude Code subscription credential from the macOS Keychain, with
    automatic refresh. Anthropic serves subscription OAuth only to its own
    client, so this mode replays Claude Code's identity block and beta set.
    Subscription credentials are not covered by Anthropic's terms for
    third-party clients, so this mode is opt-in and never the default.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
KEYCHAIN_SERVICE = "Claude Code-credentials"
API_KEY_ENV = "ANTHROPIC_API_KEY"
CC_VERSION = "2.1.283"

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_CONTEXT_WINDOW = 200_000
DEFAULT_PORT = 8090

LONG_CONTEXT_BETA = "context-1m-2025-08-07"
MODELS_WITHOUT_LONG_CONTEXT = ("claude-haiku",)

API_KEY_BETAS: tuple[str, ...] = ("prompt-caching-2024-07-31",)

CLAUDE_CODE_BETAS: tuple[str, ...] = (
    "claude-code-20250219",
    "oauth-2025-04-20",
    "interleaved-thinking-2025-05-14",
    "thinking-token-count-2026-05-13",
    "context-management-2025-06-27",
    "prompt-caching-scope-2026-01-05",
    "mid-conversation-system-2026-04-07",
    "per-turn-control-2026-07-01",
    "mid-conversation-tool-changes-2026-07-01",
    "advisor-tool-2026-03-01",
    "effort-2025-11-24",
    "extended-cache-ttl-2025-04-11",
)

# Anthropic serves subscription OAuth only for requests that carry Claude Code's
# own identity, including this billing header.
CLAUDE_CODE_IDENTITY_BLOCKS: tuple[dict[str, str], ...] = (
    {
        "type": "text",
        "text": (
            f"x-anthropic-billing-header: cc_version={CC_VERSION}.bridge; cc_entrypoint=sdk-cli;"
        ),
    },
    {
        "type": "text",
        "text": "You are Claude Code, Anthropic's official CLI for Claude.",
    },
)

CredentialMode = Literal["api-key", "claude-code"]


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


class CredentialError(RuntimeError):
    """Raised when no usable Anthropic credential is available."""


class ApiKeyCredential:
    """An Anthropic Console API key, the supported credential for third parties."""

    mode: Literal["api-key"] = "api-key"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise CredentialError(f"{API_KEY_ENV} is empty")
        self._api_key = api_key

    def token(self) -> str:  # noqa: D102 - small protocol method
        return self._api_key

    def invalidate(self) -> None:  # noqa: D102 - small protocol method
        return


class ClaudeCodeCredential:
    """Claude Code's subscription credential, with refresh and persistence."""

    mode: Literal["claude-code"] = "claude-code"

    def __init__(self, keychain_service: str = KEYCHAIN_SERVICE) -> None:
        self.keychain_service = keychain_service
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._expires_at_ms = 0.0

    def _read_keychain(self) -> dict[str, Any]:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", self.keychain_service, "-w"],
            capture_output=True,
            text=True,
        )
        if proc.returncode:
            raise CredentialError(
                f"cannot read the Claude Code credential from Keychain: {proc.stderr.strip()[:200]}"
            )
        return json.loads(proc.stdout)

    def _write_keychain(self, payload: dict[str, Any]) -> None:
        proc = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                self.keychain_service,
                "-a",
                os.environ.get("USER", "user"),
                "-w",
                json.dumps(payload),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode:
            raise CredentialError(
                f"cannot persist the refreshed credential: {proc.stderr.strip()[:200]}"
            )

    def _refresh(self, payload: dict[str, Any]) -> dict[str, Any]:
        oauth = payload.get("claudeAiOauth") or {}
        refresh_token = oauth.get("refreshToken")
        if not refresh_token:
            raise CredentialError("credential has no refresh token; run `claude` to log in")
        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": OAUTH_CLIENT_ID,
            }
        ).encode()
        request = urllib.request.Request(
            OAUTH_TOKEN_URL,
            data=body,
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                refreshed = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            raise CredentialError(
                f"token refresh failed ({exc.code}): {detail}. Run `claude` to log in again."
            ) from exc
        access_token = refreshed.get("access_token")
        if not access_token:
            raise CredentialError("token refresh returned no access_token")
        now_ms = time.time() * 1000
        oauth["accessToken"] = access_token
        oauth["expiresAt"] = int(now_ms + float(refreshed.get("expires_in", 28800)) * 1000)
        if refreshed.get("refresh_token"):
            oauth["refreshToken"] = refreshed["refresh_token"]
        if refreshed.get("refresh_token_expires_in"):
            oauth["refreshTokenExpiresAt"] = int(
                now_ms + float(refreshed["refresh_token_expires_in"]) * 1000
            )
        payload["claudeAiOauth"] = oauth
        self._write_keychain(payload)
        log("refreshed the Claude Code subscription token and persisted it")
        return payload

    def token(self) -> str:
        with self._lock:
            if self._access_token and self._expires_at_ms - time.time() * 1000 > 300_000:
                return self._access_token
            payload = self._read_keychain()
            oauth = payload.get("claudeAiOauth") or {}
            access_token = oauth.get("accessToken") or ""
            expires_at = float(oauth.get("expiresAt") or 0)
            if not access_token or expires_at - time.time() * 1000 <= 300_000:
                payload = self._refresh(payload)
                oauth = payload["claudeAiOauth"]
                access_token = oauth["accessToken"]
                expires_at = float(oauth["expiresAt"])
            self._access_token = access_token
            self._expires_at_ms = expires_at
            return access_token

    def invalidate(self) -> None:
        with self._lock:
            self._access_token = None
            self._expires_at_ms = 0.0


def resolve_credential(
    mode: str, *, api_key: str | None = None, keychain_service: str = KEYCHAIN_SERVICE
) -> ApiKeyCredential | ClaudeCodeCredential:
    """Build the credential for ``mode``; ``auto`` prefers the API key."""
    if mode == "api-key":
        return ApiKeyCredential(api_key or os.environ.get(API_KEY_ENV, ""))
    if mode == "claude-code":
        return ClaudeCodeCredential(keychain_service)
    if mode != "auto":
        raise CredentialError(f"unknown credential mode: {mode}")
    if api_key or os.environ.get(API_KEY_ENV):
        return ApiKeyCredential(api_key or os.environ[API_KEY_ENV])
    return ClaudeCodeCredential(keychain_service)


# --------------------------------------------------------------------------- #
# Request translation
# --------------------------------------------------------------------------- #


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    if content is None:
        return ""
    return json.dumps(content)


def _normalize_tool_output(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return _text_from_content(output)
    if output is None:
        return ""
    return json.dumps(output)


def anthropic_tool_name(name: str) -> str:
    """Anthropic requires tool names matching ``[a-zA-Z0-9_-]{1,64}``."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", name or "tool")
    return cleaned[:64] or "tool"


def custom_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """Express a freeform Responses custom tool as a single string parameter.

    Codex freeform tools such as ``apply_patch`` carry a grammar instead of a
    JSON schema, so the model is asked for raw text that follows that grammar.
    """
    description = str(tool.get("description") or "")
    tool_format = tool.get("format") or {}
    syntax = tool_format.get("syntax") or "text"
    definition = tool_format.get("definition") or ""
    if definition:
        description = (
            f"{description}\n\nThe input must follow this {syntax} grammar "
            f"exactly; return the raw text, not JSON-wrapped or escaped:\n{definition}"
        ).strip()
    return {
        "name": anthropic_tool_name(str(tool.get("name") or "tool")),
        "description": description or "Freeform tool input.",
        "input_schema": {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "The complete freeform input for this tool.",
                }
            },
            "required": ["input"],
        },
    }


def collect_tools(
    tools: list[Any] | None,
) -> tuple[list[dict[str, Any]], set[str], dict[str, str]]:
    """Return Anthropic tools, freeform tool names, and sanitized-name renames."""
    anthropic_tools: list[dict[str, Any]] = []
    freeform: set[str] = set()
    renames: dict[str, str] = {}
    seen: set[str] = set()

    def add(tool: dict[str, Any]) -> None:
        tool_type = tool.get("type")
        original = str(tool.get("name") or "tool")
        if tool_type == "function":
            name = anthropic_tool_name(original)
            if name in seen:
                # Anthropic rejects duplicate tool names outright, and Codex can
                # repeat a declaration across namespaces, so keep the first one.
                log(f"dropping duplicate tool declaration for {name!r}")
                return
            seen.add(name)
            if name != original:
                renames[name] = original
            anthropic_tools.append(
                {
                    "name": name,
                    "description": str(tool.get("description") or ""),
                    "input_schema": tool.get("parameters") or {"type": "object", "properties": {}},
                }
            )
        elif tool_type == "custom":
            schema = custom_tool_schema(tool)
            if schema["name"] in seen:
                log(f"dropping duplicate freeform tool declaration for {schema['name']!r}")
                return
            seen.add(schema["name"])
            if schema["name"] != original:
                renames[schema["name"]] = original
            freeform.add(schema["name"])
            anthropic_tools.append(schema)
        elif tool_type == "namespace":
            for child in tool.get("tools") or []:
                if isinstance(child, dict):
                    add(child)
        # tool_search and web_search have no Anthropic equivalent. Omitting them
        # keeps the tool list valid; the model simply cannot call them.

    for tool in tools or []:
        if isinstance(tool, dict):
            add(tool)
    return anthropic_tools, freeform, renames


def _betas_for(model: str, mode: CredentialMode) -> str:
    betas = list(API_KEY_BETAS if mode == "api-key" else CLAUDE_CODE_BETAS)
    if mode == "claude-code" and not any(
        model.startswith(prefix) for prefix in MODELS_WITHOUT_LONG_CONTEXT
    ):
        betas.insert(2, LONG_CONTEXT_BETA)
    return ",".join(betas)


def _repair_trailing_assistant(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the user turn that closes a transcript ending on assistant items.

    Orphaned ``tool_use`` blocks become error tool results, matching how Codex
    recovers interrupted tool calls. Otherwise the model gets a plain
    continuation prompt so the transcript ends on a user message.
    """
    tool_use_ids: list[str] = []
    for message in reversed(messages):
        if message["role"] != "assistant":
            break
        for block in message["content"]:
            if block.get("type") == "tool_use" and block.get("id"):
                tool_use_ids.append(str(block["id"]))
    tool_use_ids.reverse()
    if tool_use_ids:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": "Tool output unavailable: the previous turn was interrupted.",
                    "is_error": True,
                }
                for tool_use_id in tool_use_ids
            ],
        }
    return {"role": "user", "content": [{"type": "text", "text": "Continue."}]}


def message_tail_hint(messages: list[dict[str, Any]], limit: int = 4) -> str:
    """Compact digest of a payload's last messages, for bridge logs."""
    parts: list[str] = []
    for message in messages[-limit:]:
        role = "assistant" if message["role"] == "assistant" else "user"
        kinds = sorted({str(block.get("type") or "?") for block in message["content"]})
        parts.append(f"{role[0]}:{'+'.join(kinds) if kinds else 'empty'}")
    return " > ".join(parts)


def translate_request(
    body: dict[str, Any], *, mode: CredentialMode = "api-key"
) -> tuple[dict[str, Any], set[str], dict[str, str]]:
    """Convert a Responses API request body into an Anthropic Messages payload."""
    anthropic_tools, freeform, renames = collect_tools(body.get("tools"))

    system_blocks: list[dict[str, Any]] = []
    if mode == "claude-code":
        system_blocks.extend(dict(block) for block in CLAUDE_CODE_IDENTITY_BLOCKS)
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        system_blocks.append({"type": "text", "text": instructions})

    messages: list[dict[str, Any]] = []

    def push(message: dict[str, Any]) -> None:
        if not message["content"]:
            return
        if (
            message["role"] == "user"
            and messages
            and messages[-1]["role"] == "user"
            and all(block.get("type") == "tool_result" for block in message["content"])
            and all(block.get("type") == "tool_result" for block in messages[-1]["content"])
        ):
            # Anthropic wants every tool_result for one assistant turn grouped
            # into a single user message.
            messages[-1]["content"].extend(message["content"])
            return
        messages.append(message)

    for item in body.get("input") or []:
        if not isinstance(item, dict):
            push({"role": "user", "content": [{"type": "text", "text": str(item)}]})
            continue
        item_type = item.get("type")

        if item_type == "message":
            blocks: list[dict[str, Any]] = []
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    blocks.append({"type": "text", "text": str(part)})
                    continue
                part_type = part.get("type")
                if part_type in ("input_text", "output_text", "text"):
                    blocks.append({"type": "text", "text": str(part.get("text") or "")})
                elif part_type == "input_image":
                    image_url = str(part.get("image_url") or "")
                    if image_url.startswith("data:"):
                        header, _, data = image_url.partition(",")
                        media = header[5:].split(";")[0] or "image/png"
                        blocks.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media,
                                    "data": data,
                                },
                            }
                        )
            if blocks:
                role = item.get("role") or "user"
                push(
                    {
                        "role": "assistant" if role in ("assistant", "model") else "user",
                        "content": blocks,
                    }
                )

        elif item_type in ("function_call", "custom_tool_call"):
            call_id = str(item.get("call_id") or item.get("id") or "call")
            name = anthropic_tool_name(str(item.get("name") or "tool"))
            if item_type == "custom_tool_call":
                tool_input: Any = {"input": str(item.get("input") or "")}
            else:
                raw = item.get("arguments")
                try:
                    tool_input = json.loads(raw) if isinstance(raw, str) else (raw or {})
                except json.JSONDecodeError:
                    tool_input = {"input": raw}
                if not isinstance(tool_input, dict):
                    tool_input = {"input": tool_input}
            push(
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}
                    ],
                }
            )

        elif item_type in ("function_call_output", "custom_tool_call_output"):
            call_id = str(item.get("call_id") or item.get("id") or "call")
            output = _normalize_tool_output(item.get("output"))
            push(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "content": output or "(no output)",
                        }
                    ],
                }
            )

        elif item_type == "reasoning":
            # Anthropic rejects replayed thinking it did not sign in this turn,
            # so reasoning items from history are dropped.
            continue

    if not messages:
        messages = [{"role": "user", "content": [{"type": "text", "text": ""}]}]

    # Claude models reject assistant prefills: the transcript must end on a user
    # message. Codex can hand us one that does not, e.g. when a turn is
    # interrupted after a tool call and the transcript is replayed. Leave the
    # recorded turn alone and append the follow-up turn the model needs.
    if messages[-1]["role"] == "assistant":
        original_tail = message_tail_hint(messages)
        repair = _repair_trailing_assistant(messages)
        log(
            "trailing assistant turn repaired for Anthropic: "
            f"{len(repair['content'])} block(s) appended after {original_tail}"
        )
        messages.append(repair)

    payload: dict[str, Any] = {
        "model": body.get("model") or DEFAULT_MODEL,
        "max_tokens": int(body.get("max_output_tokens") or 32000),
        "messages": messages,
        "stream": True,
    }
    if system_blocks:
        payload["system"] = system_blocks
    if anthropic_tools:
        payload["tools"] = anthropic_tools
        tool_choice = body.get("tool_choice")
        if tool_choice == "none":
            payload["tool_choice"] = {"type": "none"}
        elif tool_choice == "required":
            payload["tool_choice"] = {"type": "any"}
        else:
            payload["tool_choice"] = {"type": "auto"}
    return payload, freeform, renames


def anthropic_headers(token: str, model: str, mode: CredentialMode) -> dict[str, str]:
    """Headers Claude/Codex traffic uses; subscription mode mirrors Claude Code."""
    headers = {
        "anthropic-version": "2023-06-01",
        "anthropic-beta": _betas_for(model, mode),
        "content-type": "application/json",
        "accept": "text/event-stream",
    }
    if mode == "claude-code":
        headers["authorization"] = f"Bearer {token}"
        headers["user-agent"] = f"claude-cli/{CC_VERSION} (external, sdk-cli)"
        headers["x-app"] = "cli"
        headers["anthropic-dangerous-direct-browser-access"] = "true"
    else:
        headers["x-api-key"] = token
    return headers


def anthropic_stream(
    credentials: ApiKeyCredential | ClaudeCodeCredential,
    payload: dict[str, Any],
    *,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(event_type, data)`` pairs from Anthropic's SSE stream."""
    model = str(payload.get("model") or DEFAULT_MODEL)
    request = urllib.request.Request(
        ANTHROPIC_MESSAGES_URL,
        data=json.dumps(payload).encode(),
        method="POST",
        headers=anthropic_headers(credentials.token(), model, credentials.mode),
    )
    try:
        response = urlopen(request, timeout=900)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        if exc.code in (401, 403):
            credentials.invalidate()
        raise CredentialError(f"anthropic {exc.code}: {detail[:400]}") from exc

    with response:
        event_type = ""
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
                if not data:
                    continue
                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    continue
                yield event_type or str(parsed.get("type") or "message"), parsed
            elif not line:
                event_type = ""


# --------------------------------------------------------------------------- #
# Stream translation
# --------------------------------------------------------------------------- #


class ResponsesStream:
    """Builds Responses API SSE events from an Anthropic message stream."""

    def __init__(
        self,
        response_id: str,
        model: str,
        freeform: set[str] | None = None,
        renames: dict[str, str] | None = None,
    ) -> None:
        self.response_id = response_id
        self.model = model
        self.freeform = freeform or set()
        self.renames = renames or {}
        self.blocks: dict[int, dict[str, Any]] = {}
        self.items: list[dict[str, Any]] = []
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.stop_reason: str | None = None
        self.item_counter = 0
        # Item ids are recorded into the Codex transcript, so they must stay
        # unique across the requests of one turn. Scope them to the response.
        self.item_scope = response_id.removeprefix("resp_")[:10] or "0"

    def _next_item_id(self, prefix: str) -> str:
        self.item_counter += 1
        return f"{prefix}_{self.item_scope}_{self.item_counter}"

    def _output_index(self, block: dict[str, Any]) -> int:
        """Assign an item's response output index when it is first emitted."""
        index = block.get("output_index")
        if index is None:
            index = len(self.items)
            block["output_index"] = index
        return int(index)

    @staticmethod
    def sse(event: str, data: dict[str, Any]) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()

    def feed(self, event_type: str, data: dict[str, Any]) -> list[bytes]:
        handler = getattr(self, f"_on_{event_type}", None)
        if handler is None:
            return []
        return list(handler(data) or [])

    # -- handlers ----------------------------------------------------------- #

    def _on_message_start(self, data: dict[str, Any]) -> Iterator[bytes]:
        usage = (data.get("message") or {}).get("usage") or {}
        self.usage["input_tokens"] = int(usage.get("input_tokens") or 0)
        self.usage["output_tokens"] = int(usage.get("output_tokens") or 0)
        yield self.sse(
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

    def _on_content_block_start(self, data: dict[str, Any]) -> Iterator[bytes]:
        index = int(data.get("index") or 0)
        block = data.get("content_block") or {}
        block_type = block.get("type")
        if block_type == "text":
            self.blocks[index] = {
                "kind": "text",
                "item_id": self._next_item_id("msg"),
                "text": str(block.get("text") or ""),
                "output_index": None,
                "announced": False,
                "done": False,
            }
        elif block_type == "tool_use":
            name = str(block.get("name") or "tool")
            self.blocks[index] = {
                "kind": "tool_use",
                "item_id": self._next_item_id("ctc" if name in self.freeform else "fc"),
                "call_id": str(block.get("id") or "call"),
                "name": name,
                "json": "",
                "output_index": None,
            }
        elif block_type in ("thinking", "redacted_thinking"):
            self.blocks[index] = {"kind": "thinking", "output_index": len(self.items)}
        return
        yield  # pragma: no cover - keeps this handler a generator

    def _on_content_block_delta(self, data: dict[str, Any]) -> Iterator[bytes]:
        block = self.blocks.get(int(data.get("index") or 0))
        if block is None:
            return
        delta = data.get("delta") or {}
        delta_type = delta.get("type")
        if block["kind"] == "text" and delta_type == "text_delta":
            text = str(delta.get("text") or "")
            block["text"] += text
            if text:
                output_index = self._output_index(block)
                if not block["announced"]:
                    # Codex tracks an "active item" for text deltas and panics
                    # when a delta arrives before the item is announced.
                    block["announced"] = True
                    yield self.sse(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "output_index": output_index,
                            "item": {
                                "type": "message",
                                "id": block["item_id"],
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": ""}],
                            },
                        },
                    )
                yield self.sse(
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "item_id": block["item_id"],
                        "output_index": output_index,
                        "content_index": 0,
                        "delta": text,
                    },
                )
        elif block["kind"] == "tool_use" and delta_type == "input_json_delta":
            block["json"] += str(delta.get("partial_json") or "")

    def _on_content_block_stop(self, data: dict[str, Any]) -> Iterator[bytes]:
        block = self.blocks.get(int(data.get("index") or 0))
        if block is None:
            return
        if block.get("kind") == "text":
            # Close the assistant text item as soon as its content block ends.
            #
            # Codex records items in arrival order, so deferring this to
            # ``message_stop`` put the text *after* the tool calls of the same
            # response. That duplicated the text in the TUI and left the
            # transcript ending on an assistant item, which Anthropic rejects
            # with "The conversation must end with a user message."
            yield from self._emit_text_item(block)
            return
        if block.get("kind") != "tool_use":
            return
        try:
            parsed = json.loads(block["json"]) if block["json"].strip() else {}
        except json.JSONDecodeError:
            parsed = {"input": block["json"]}
        if not isinstance(parsed, dict):
            parsed = {"input": parsed}
        output_index = self._output_index(block)
        name = block["name"]
        original = self.renames.get(name, name)
        if name in self.freeform:
            raw_input = parsed.get("input")
            if not isinstance(raw_input, str):
                raw_input = json.dumps(raw_input) if raw_input is not None else block["json"]
            item = {
                "type": "custom_tool_call",
                "id": block["item_id"],
                "call_id": block["call_id"],
                "name": original,
                "input": raw_input,
            }
            # Codex renders freeform tool input from this delta event.
            yield self.sse(
                "response.custom_tool_call_input.delta",
                {
                    "type": "response.custom_tool_call_input.delta",
                    "item_id": block["item_id"],
                    "call_id": block["call_id"],
                    "delta": raw_input,
                },
            )
        else:
            item = {
                "type": "function_call",
                "id": block["item_id"],
                "call_id": block["call_id"],
                "name": original,
                "arguments": json.dumps(parsed),
            }
        self.items.append(item)
        yield self.sse(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": item,
            },
        )
        yield self.sse(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": output_index,
                "item": item,
            },
        )

    def _on_message_delta(self, data: dict[str, Any]) -> Iterator[bytes]:
        usage = data.get("usage") or {}
        if usage.get("output_tokens") is not None:
            self.usage["output_tokens"] = int(usage["output_tokens"])
        stop_reason = (data.get("delta") or {}).get("stop_reason")
        if stop_reason:
            self.stop_reason = str(stop_reason)
        return
        yield  # pragma: no cover - keeps this handler a generator

    def _emit_text_item(self, block: dict[str, Any]) -> Iterator[bytes]:
        """Emit a finished assistant text block as a completed message item."""
        if block.get("done"):
            return
        block["done"] = True
        text = str(block.get("text") or "")
        if not text:
            return
        output_index = self._output_index(block)
        if not block.get("announced"):
            # A block can carry its whole text in ``content_block_start``.
            # Announce it so Codex has an active item for the completed text.
            block["announced"] = True
            yield self.sse(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": {
                        "type": "message",
                        "id": block["item_id"],
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": ""}],
                    },
                },
            )
        item = {
            "type": "message",
            "id": block["item_id"],
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
        self.items.append(item)
        yield self.sse(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": output_index,
                "item": item,
            },
        )

    def _text_items(self) -> Iterator[bytes]:
        for index in sorted(self.blocks):
            block = self.blocks[index]
            if block.get("kind") != "text":
                continue
            yield from self._emit_text_item(block)

    def _on_message_stop(self, data: dict[str, Any]) -> Iterator[bytes]:
        yield from self._text_items()
        total = self.usage["input_tokens"] + self.usage["output_tokens"]
        yield self.sse(
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "id": self.response_id,
                    "object": "response",
                    "status": "completed",
                    "model": self.model,
                    "output": self.items,
                    "usage": {
                        "input_tokens": self.usage["input_tokens"],
                        "input_tokens_details": None,
                        "output_tokens": self.usage["output_tokens"],
                        "output_tokens_details": None,
                        "total_tokens": total,
                    },
                },
            },
        )

    def _on_error(self, data: dict[str, Any]) -> Iterator[bytes]:
        error = data.get("error") or {}
        yield self.sse(
            "response.failed",
            {
                "type": "response.failed",
                "response": {
                    "id": self.response_id,
                    "status": "failed",
                    "error": {
                        "code": str(error.get("type") or "api_error"),
                        "message": str(error.get("message") or "unknown error"),
                    },
                },
            },
        )


# --------------------------------------------------------------------------- #
# Model catalog
# --------------------------------------------------------------------------- #


def catalog_from_config(config: Any, backend_name: str = "claude") -> list[tuple[str, int]]:
    """Derive ``(slug, context_window)`` entries from an AgentRoute backend."""
    backend = getattr(config, "backends", {}).get(backend_name)
    if backend is None:
        return [(DEFAULT_MODEL, DEFAULT_CONTEXT_WINDOW)]
    seen: list[str] = []
    for tier in ("fast", "normal", "smart", "max"):
        target = backend.tiers.get(tier)
        if target and target.model not in seen:
            seen.append(target.model)
    return [(model, DEFAULT_CONTEXT_WINDOW) for model in seen] or [
        (DEFAULT_MODEL, DEFAULT_CONTEXT_WINDOW)
    ]


def model_catalog(
    models: list[tuple[str, int]] | None = None,
) -> dict[str, Any]:
    """Build the Codex model descriptor list for ``GET /v1/models``."""
    entries = models or [(DEFAULT_MODEL, DEFAULT_CONTEXT_WINDOW)]
    descriptors = [
        {
            "slug": slug,
            "display_name": slug,
            "description": "Claude served through the AgentRoute bridge.",
            "model_messages": {
                "instructions_template": (
                    "You are a coding model in Codex CLI. Follow the system and tool "
                    "instructions in each request exactly. Use tools for file and "
                    "command work, and verify changes before reporting completion."
                )
            },
            "default_reasoning_level": None,
            "supported_reasoning_levels": [],
            "shell_type": "unified_exec",
            "visibility": "list",
            "supported_in_api": True,
            "priority": 40,
            "availability_nux": None,
            "upgrade": None,
            "default_reasoning_summary": "none",
            "support_verbosity": False,
            "default_verbosity": None,
            "apply_patch_tool_type": "freeform",
            "truncation_policy": {"mode": "bytes", "limit": 10000},
            "effective_context_window_percent": 95,
            "experimental_supported_tools": [],
            "supports_reasoning_summary_parameter": False,
            "context_window": context_window,
            "max_context_window": context_window,
            "input_modalities": ["text"],
            "supports_search_tool": False,
            "supports_experimental_context": False,
            "use_responses_lite": False,
            "supports_reasoning_effort_updates": False,
            "node_repl_auto_review_required": False,
            "node_repl_disabled": False,
        }
        for slug, context_window in entries
    ]
    return {
        "object": "list",
        "data": [{"id": slug} for slug, _ in entries],
        "models": descriptors,
    }


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #


class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "agentroute-claude-bridge"
    credentials: ApiKeyCredential | ClaudeCodeCredential
    models: list[tuple[str, int]] = [(DEFAULT_MODEL, DEFAULT_CONTEXT_WINDOW)]
    default_model: str = DEFAULT_MODEL
    # staticmethod keeps instance access from binding `self` as the first
    # argument, which would call anthropic_stream(handler, credentials, payload).
    stream_factory: Callable[..., Any] = staticmethod(anthropic_stream)
    logger: Callable[[str], None] = staticmethod(log)

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _start_sse(self) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()

    def _chunk(self, data: bytes) -> bool:
        try:
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _end_chunks(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?")[0]
        if path in ("/healthz", "/health"):
            self._json(200, {"status": "ok"})
        elif path.endswith("/models"):
            self._json(200, model_catalog(self.models))
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if not self.path.split("?")[0].endswith("/responses"):
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._json(400, {"error": f"invalid JSON: {exc}"})
            return

        model = str(body.get("model") or self.default_model)
        try:
            payload, freeform, renames = translate_request(
                {**body, "model": model}, mode=self.credentials.mode
            )
        except Exception as exc:  # noqa: BLE001 - report translation faults to the client
            self.logger(f"translation failed: {exc}")
            self._json(400, {"error": f"translation failed: {exc}"})
            return

        response_id = f"resp_{hashlib.sha256(raw + str(time.time()).encode()).hexdigest()[:24]}"
        stream = ResponsesStream(response_id, model, freeform, renames)
        started = False
        stop_reason = None
        self.logger(
            f"{model}: {len(payload.get('messages') or [])} messages, "
            f"{len(payload.get('tools') or [])} tools, "
            f"tail={message_tail_hint(payload.get('messages') or [])}"
        )
        try:
            for event_type, data in self.stream_factory(self.credentials, payload):
                if event_type == "message_delta":
                    stop_reason = (data.get("delta") or {}).get("stop_reason") or stop_reason
                if not started:
                    self._start_sse()
                    started = True
                for chunk in stream.feed(event_type, data):
                    if not self._chunk(chunk):
                        return
        except CredentialError as exc:
            self.logger(f"upstream error for {model}: {exc}")
            if not started:
                self._json(502, {"error": str(exc)})
                return
            for chunk in stream.feed(
                "error", {"error": {"type": "api_error", "message": str(exc)}}
            ):
                self._chunk(chunk)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the client
            self.logger(f"bridge failure for {model}: {type(exc).__name__}: {exc}")
            if not started:
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
                return
        finally:
            if started:
                self._end_chunks()
            self.logger(f"{model}: finished, stop_reason={stop_reason}")


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        # http.server's own server_bind calls socket.getfqdn(), which stalls on
        # hosts whose loopback address has no usable PTR record.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


def serve(
    credentials: ApiKeyCredential | ClaudeCodeCredential,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    models: list[tuple[str, int]] | None = None,
    default_model: str | None = None,
) -> None:
    """Run the bridge until interrupted."""
    entries = models or [(DEFAULT_MODEL, DEFAULT_CONTEXT_WINDOW)]
    BridgeHandler.credentials = credentials
    BridgeHandler.models = entries
    BridgeHandler.default_model = default_model or entries[0][0]
    server = BridgeServer((host, port), BridgeHandler)
    log(
        f"claude bridge listening on http://{host}:{port}/v1 "
        f"({credentials.mode} credential, models: {', '.join(m for m, _ in entries)})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        server.server_close()
