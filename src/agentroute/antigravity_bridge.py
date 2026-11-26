"""Gemini through Antigravity's existing OAuth credential.

Codex speaks the Responses API. Antigravity serves Gemini through its own
entitlement-gated endpoint, and replaying that wire protocol from a third-party
client is rejected by the service. This bridge instead delegates inference to
the installed ``agy`` client, which already holds the OAuth credential and the
user's Antigravity quota, and translates the result into Responses events.

The credential comes from the macOS Keychain entry ``service=gemini,
account=antigravity``. That token carries the ``auth/aicode`` scope Antigravity
requires. ``~/.gemini/oauth_creds.json`` belongs to the Gemini CLI OAuth client,
which lacks that scope, so it is accepted only when it actually satisfies it.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

KEYCHAIN_SERVICE = "gemini"
KEYCHAIN_ACCOUNT = "antigravity"
AICODE_SCOPE = "https://www.googleapis.com/auth/aicode"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

# Model ids as ``agy models`` reports them for the signed-in account.
MODELS: dict[str, str] = {
    "fast": "gemini-3.8-flash-low",
    "normal": "gemini-3.8-flash-medium",
    "smart": "gemini-3.8-flash-high",
    "max": "gemini-4-hydrogen-high",
}
CONTEXT_WINDOW = 1_000_000
DEFAULT_AGY = "agy"
RESPONSES_PATH = "/v1/responses"


def log(message: str) -> None:
    """Operational log line. Never includes prompts, output, or credentials."""
    print(f"agentroute-antigravity: {message}", flush=True)


class CredentialError(RuntimeError):
    """The Antigravity OAuth credential is missing, stale, or unusable."""


def _decode_keychain_payload(raw: str) -> dict[str, Any]:
    """Decode the ``<prefix>:<base64 json>`` form the Antigravity client stores."""
    _, separator, payload = raw.strip().partition(":")
    if not separator or not payload:
        raise CredentialError("the Antigravity Keychain entry has an unexpected format")
    try:
        decoded = base64.b64decode(payload + "=" * (-len(payload) % 4))
        body = json.loads(decoded.decode())
    except (ValueError, json.JSONDecodeError) as exc:
        raise CredentialError(f"cannot decode the Antigravity Keychain entry: {exc}") from exc
    token = body.get("token")
    if not isinstance(token, dict) or not token.get("access_token"):
        raise CredentialError("the Antigravity Keychain entry carries no access token")
    return {
        "access_token": str(token["access_token"]),
        "refresh_token": token.get("refresh_token"),
        "expiry": token.get("expiry"),
        "auth_method": body.get("auth_method"),
        "source": "keychain",
    }


def read_keychain_credential() -> dict[str, Any]:
    """Read the Antigravity credential the signed-in client maintains."""
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
        capture_output=True,
        text=True,
    )
    if proc.returncode:
        raise CredentialError("no Antigravity credential in Keychain; run `agy` and sign in once")
    return _decode_keychain_payload(proc.stdout)


def read_file_credential(path: str | None = None) -> dict[str, Any]:
    """Read the Gemini CLI credential file, accepting it only with the aicode scope."""
    target = path or os.path.expanduser("~/.gemini/oauth_creds.json")
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise CredentialError("no Antigravity credential file; run `agy` and sign in once") from exc
    except (OSError, ValueError) as exc:
        raise CredentialError(f"cannot read the Gemini credential file: {exc}") from exc
    if AICODE_SCOPE not in str(data.get("scope") or ""):
        raise CredentialError(
            "the credential file belongs to the Gemini CLI client and lacks the "
            "auth/aicode scope; sign in with `agy` so the Keychain entry exists"
        )
    return {
        "access_token": str(data.get("access_token") or ""),
        "refresh_token": data.get("refresh_token"),
        "expiry": data.get("expiry_date"),
        "auth_method": "consumer",
        "source": "file",
    }


def credential_scope(access_token: str) -> str:
    """Return the granted scope string, or raise when the token is not usable."""
    request = urllib.request.Request(
        f"{TOKENINFO_URL}?access_token={access_token}",
        headers={"user-agent": "agentroute-antigravity"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise CredentialError(
            "the Antigravity access token expired; run `agy` once to refresh it"
        ) from exc
    except (urllib.error.URLError, ValueError) as exc:
        raise CredentialError(f"cannot validate the Antigravity token: {exc}") from exc
    if "error" in body:
        raise CredentialError("the Antigravity access token is no longer valid; run `agy` once")
    return str(body.get("scope") or "")


@dataclass
class AntigravityCredential:
    """Antigravity's own OAuth credential, validated for the aicode scope."""

    mode: str = "antigravity"

    def token(self) -> str:
        last: CredentialError | None = None
        for reader in (read_keychain_credential, read_file_credential):
            try:
                data = reader()
            except CredentialError as exc:
                last = exc
                continue
            token = data["access_token"]
            if not token:
                continue
            if AICODE_SCOPE in credential_scope(token):
                return token
            last = CredentialError(
                "the stored Antigravity credential lacks the auth/aicode scope; "
                "run `agy` once to sign in again"
            )
        raise last or CredentialError("no usable Antigravity credential")

    def source(self) -> str:
        for reader in (read_keychain_credential, read_file_credential):
            try:
                return str(reader().get("source"))
            except CredentialError:
                continue
        return "missing"


def _content_text(content: Any) -> str:
    """Flatten Responses content items into plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(part for part in parts if part)


def build_prompt(body: dict[str, Any]) -> str:
    """Render a Responses request as one delegate prompt."""
    sections: list[str] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        sections.append(instructions.strip())
    input_items = body.get("input")
    if isinstance(input_items, str):
        sections.append(input_items.strip())
    elif isinstance(input_items, list):
        for item in input_items:
            if not isinstance(item, dict):
                continue
            if item.get("type") in {"function_call_output", "custom_tool_call_output"}:
                output = _content_text(item.get("output"))
                if output:
                    sections.append(f"Tool result ({item.get('call_id', 'unknown')}):\n{output}")
                continue
            text = _content_text(item.get("content"))
            if not text and isinstance(item.get("text"), str):
                text = item["text"]
            if text.strip():
                role = str(item.get("role") or "user").capitalize()
                sections.append(f"{role}: {text.strip()}")
    # ``agy`` runs its own tool loop, so a delegate cannot execute Codex's tools.
    if body.get("tools"):
        sections.append(
            "Answer directly. This delegate session cannot call the caller's tools, "
            "so describe any command or patch the caller should apply."
        )
    return "\n\n".join(sections).strip() or "Continue."


def parse_agy_output(stdout: str) -> dict[str, Any]:
    """Parse ``agy --output-format json`` output into text, usage, and status."""
    text = stdout.strip()
    try:
        payload = json.loads(text)
    except ValueError:
        return {"text": text, "usage": None, "status": "unknown", "error": None}
    if not isinstance(payload, dict):
        return {"text": text, "usage": None, "status": "unknown", "error": None}
    response = payload.get("response")
    usage = payload.get("usage")
    return {
        "text": response if isinstance(response, str) else "",
        "usage": usage if isinstance(usage, dict) else None,
        "status": str(payload.get("status") or "unknown"),
        "error": payload.get("error"),
    }


def classify_failure(message: str) -> int:
    """Map an upstream Antigravity failure onto an HTTP status for Codex."""
    if re.search(r"quota|rate limit|resource_exhausted|limit reached", message, re.I):
        return 429
    return 502


def run_agy(
    prompt: str,
    model: str,
    *,
    executable: str = DEFAULT_AGY,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """Run one headless Antigravity turn and return its parsed result."""
    command = [
        executable,
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "json",
        "--disable-slash-commands",
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise CredentialError(f"the Antigravity CLI is not installed at {executable!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"Antigravity did not answer within {timeout:.0f}s") from exc
    parsed = parse_agy_output(proc.stdout)
    if parsed["status"] == "ERROR":
        detail = str(parsed.get("error") or "Antigravity reported an error")[:300]
        raise RuntimeError(detail)
    if proc.returncode and not parsed["text"]:
        lines = (proc.stderr or "").strip().splitlines()
        raise RuntimeError((lines[-1] if lines else f"agy exited with {proc.returncode}")[:300])
    return parsed


def usage_from(usage: dict[str, Any] | None) -> dict[str, int]:
    """Map Antigravity usage counters onto Responses usage fields."""
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or (input_tokens + output_tokens))
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total}


def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def response_events(text: str, model: str, usage: dict[str, Any] | None) -> Iterator[bytes]:
    """Emit a complete Responses transcript for one finished delegate turn."""
    response_id = f"resp_{uuid.uuid4().hex}"
    item_id = f"msg_{uuid.uuid4().hex}"
    yield sse(
        "response.created",
        {
            "type": "response.created",
            "response": {
                "id": response_id,
                "object": "response",
                "status": "in_progress",
                "model": model,
            },
        },
    )
    yield sse(
        "response.output_item.added",
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "message",
                "id": item_id,
                "role": "assistant",
                "content": [{"type": "output_text", "text": ""}],
            },
        },
    )
    yield sse(
        "response.content_part.added",
        {
            "type": "response.content_part.added",
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": ""},
        },
    )
    if text:
        yield sse(
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "delta": text,
            },
        )
    yield sse(
        "response.output_text.done",
        {
            "type": "response.output_text.done",
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "text": text,
        },
    )
    item = {
        "type": "message",
        "id": item_id,
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    yield sse(
        "response.output_item.done",
        {"type": "response.output_item.done", "output_index": 0, "item": item},
    )
    yield sse(
        "response.completed",
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "object": "response",
                "status": "completed",
                "model": model,
                "output": [item],
                "usage": usage_from(usage),
            },
        },
    )


def completion_body(text: str, model: str, usage: dict[str, Any] | None) -> dict[str, Any]:
    """Build the non-streaming Responses payload."""
    return {
        "id": f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "status": "completed",
        "model": model,
        "output": [
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex}",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": usage_from(usage),
    }


class AntigravityHandler(BaseHTTPRequestHandler):
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
            self._json(
                200,
                {
                    "provider": "antigravity",
                    "credential_source": AntigravityCredential().source(),
                    "models": MODELS,
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
        try:
            result = run_agy(build_prompt(body), model)
        except CredentialError as exc:
            self._json(503, {"error": str(exc)})
            return
        except TimeoutError as exc:
            self._json(504, {"error": str(exc)})
            return
        except RuntimeError as exc:
            self._json(classify_failure(str(exc)), {"error": str(exc)})
            return
        text = str(result.get("text") or "")
        usage = result.get("usage")
        if not body.get("stream"):
            self._json(200, completion_body(text, model, usage))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        for chunk in response_events(text, model, usage):
            self.wfile.write(chunk)
        self.close_connection = True


class AntigravityServer(ThreadingHTTPServer):
    daemon_threads = True


def serve(host: str = "127.0.0.1", port: int = 8816) -> None:
    server = AntigravityServer((host, port), AntigravityHandler)
    log(f"listening on http://{host}:{port}/v1")
    server.serve_forever()

