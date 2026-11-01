"""Loopback Responses proxy for the subscription authenticated by the Grok CLI.

The CLI alone owns OAuth refresh and credential writes. No tokens enter AgentRoute
configuration, command arguments, logs, or provider catalogs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .config import ExecutionBackendConfig, ModelCapabilities, ModelTarget

UPSTREAM = "https://cli-chat-proxy.grok.com/v1"
DEFAULT_PORT = 8092
MAX_BODY = 50 * 1024 * 1024
MODELS = ("grok-4.7-build-fast", "grok-4.6", "grok-4.7", "grok-4.5")


class GrokCredential:
    def __init__(self, binary: str, home: Path | None = None):
        self.binary = binary
        self.home = home or Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok")))
        self.lock = threading.Lock()

    def _read(self):
        path = Path(os.environ.get("GROK_AUTH_PATH", str(self.home / "auth.json")))
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("Invalid Grok login; run grok login")
        # Select only first-party Grok credentials; custom enterprise issuers must
        # never have their tokens forwarded to the Grok subscription endpoint.
        candidates = [
            credential
            for scope, credential in data.items()
            if isinstance(scope, str)
            and scope.startswith("https://auth.x.ai::")
            and isinstance(credential, dict)
            and credential.get("key")
        ]
        if len(candidates) == 1:
            return candidates[0]
        raise ValueError("No Grok subscription login; run grok login")

    def token(self):
        with self.lock:
            credential = self._read()
            expiry = datetime.fromisoformat(credential["expires_at"].replace("Z", "+00:00"))
            if (expiry - datetime.now(timezone.utc)).total_seconds() < 120:
                # Delegate refresh/locking/rotation to the credential's owner.
                refresh = subprocess.Popen(
                    [self.binary, "models"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                try:
                    code = refresh.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    # Killing the CLI during refresh can lose its rotated token.
                    threading.Thread(target=refresh.wait, daemon=True).start()
                    raise ValueError("Grok refresh still running; retry shortly") from None
                if code:
                    raise ValueError("Grok refresh failed; run grok models")
                credential = self._read()
                expiry = datetime.fromisoformat(credential["expires_at"].replace("Z", "+00:00"))
                if expiry <= datetime.now(timezone.utc):
                    raise ValueError("Grok login expired; run grok login")
            return credential["key"]

    def version(self):
        result = subprocess.run(
            [self.binary, "--version"], capture_output=True, text=True, timeout=10, check=True
        )
        match = re.search(r"\b(\d+\.\d+\.\d+)\b", result.stdout)
        if not match:
            raise ValueError("Cannot determine installed Grok CLI version")
        return match[1]


def ensure_backend(config, port=DEFAULT_PORT):
    if "grok" not in config.backends:
        config.backends["grok"] = ExecutionBackendConfig(
            enabled=True,
            codex_provider="agentroute-grok",
            display_name="Grok subscription (Grok CLI)",
            base_url=f"http://127.0.0.1:{port}/v1",
            tool_compatibility="functions_and_apply_patch_preserve_reasoning",
            review_model=MODELS[0],
            tiers={
                tier: ModelTarget(model=model, reasoning_effort=effort)
                for tier, model, effort in (
                    ("fast", MODELS[0], "low"),
                    ("normal", "grok-4.6", "medium"),
                    ("smart", "grok-4.7", "high"),
                    ("max", "grok-4.7", "xhigh"),
                )
            },
        )
    else:
        config.backends["grok"].enabled = True
        config.backends["grok"].base_url = f"http://127.0.0.1:{port}/v1"
    for model in MODELS:
        config.capabilities.models.setdefault(
            model,
            ModelCapabilities(
                tool_calling="apply_patch_only", reasoning=True, vision=True, context_window=256000
            ),
        )


def request_headers(credential, version, model):
    return {
        "Authorization": "Bearer " + credential.token(),
        "Content-Type": "application/json",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-grok-model-override": model,
        "x-grok-client-version": version,
        "x-grok-client-identifier": "grok-shell",
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def open_upstream(request, *, timeout):
    # Credentials must never follow a redirect to another origin.
    return urllib.request.build_opener(NoRedirect).open(request, timeout=timeout)


class GrokServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, credential):
        self.credential = credential
        self.cli_version = credential.version()
        super().__init__(("127.0.0.1", port), GrokHandler)


class GrokHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Do not record request bodies, credentials, or model output.

    def _json(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.headers.get("Origin") or self.headers.get("Host", "").split(":")[0] not in {
            "127.0.0.1",
            "localhost",
        }:
            self._json(403, {"error": "Browser origins are not accepted"})
            return
        if self.path == "/healthz":
            self._json(200, {"provider": "grok", "version": __version__})
        elif self.path == "/usage":
            from .grok_usage import fetch_usage

            self._json(200, fetch_usage(self.server.credential, self.server.cli_version))
        elif self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": m} for m in MODELS]})
        else:
            self._json(404, {"error": "Unknown route"})

    def do_POST(self):
        if self.path != "/v1/responses":
            self._json(404, {"error": "Unknown route"})
            return
        if self.headers.get("Origin") or self.headers.get("Host", "").split(":")[0] not in {
            "127.0.0.1",
            "localhost",
        }:
            self._json(403, {"error": "Browser origins are not accepted"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict) or body.get("model") not in MODELS:
                raise ValueError("Unknown Grok model")
            body["stream"] = True
            body["store"] = False
            names = tool_names(body)
            body = translate_request(body)
            headers = request_headers(
                self.server.credential, self.server.cli_version, body["model"]
            )
            from .grok_usage import account_key

            account = account_key(self.server.credential)
            request = urllib.request.Request(
                UPSTREAM + self.path.removeprefix("/v1"),
                data=json.dumps(body).encode(),
                headers=headers,
            )
            upstream = open_upstream(request, timeout=300)
        except (ValueError, KeyError, TypeError):
            self._json(400, {"error": "Invalid request or Grok login; run grok login"})
            return
        except urllib.error.HTTPError as error:
            if error.code == 429:
                import time

                from .grok_usage import write_snapshot

                try:
                    cooldown = max(1, min(3600, int(error.headers.get("Retry-After", "60"))))
                except (ValueError, TypeError):
                    cooldown = 60
                try:
                    write_snapshot(
                        self.server.credential,
                        {"blocked_until": time.time() + cooldown},
                        account=account,
                    )
                except (OSError, ValueError):
                    pass
            # Never expose upstream diagnostics, which may contain prompt data.
            self._json(error.code, {"error": f"Grok upstream HTTP {error.code}"})
            error.close()
            return
        except (OSError, subprocess.SubprocessError):
            self._json(502, {"error": "Grok unavailable; check grok login and grok models"})
            return
        with upstream:
            self.send_response(200)
            self.send_header(
                "Content-Type", upstream.headers.get("Content-Type", "text/event-stream")
            )
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                for chunk in translate_stream(upstream, names):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                return


PATCH_FUNCTION = "agentroute_apply_patch"


def translate_request(body):
    """Encode Codex's freeform patch tool as an ordinary function."""
    body = json.loads(json.dumps(body))
    # OpenAI account service tiers are not Grok subscription entitlements.
    body = {
        key: value
        for key, value in body.items()
        if key
        in {
            "model",
            "input",
            "instructions",
            "tools",
            "reasoning",
            "stream",
            "store",
            "include",
            "parallel_tool_calls",
            "max_output_tokens",
            "tool_choice",
            "temperature",
            "top_p",
        }
    }

    body["tools"] = flattened_tools(body.get("tools", []))
    for tool in body["tools"]:
        if tool.get("type") == "web_search":
            # Codex's OpenAI-only toggle is rejected by Grok's search tool.
            tool.pop("external_web_access", None)
        if tool.get("type") == "custom":
            if tool.get("name") != "apply_patch":
                raise ValueError("Unsupported custom tool")
            description = (
                tool.get("description", "") + "\n" + tool.get("format", {}).get("definition", "")
            )
            tool.clear()
            tool.update(
                type="function",
                name=PATCH_FUNCTION,
                description=description + "\nSupply the complete patch text in input.",
                parameters={
                    "type": "object",
                    "properties": {"input": {"type": "string"}},
                    "required": ["input"],
                    "additionalProperties": False,
                },
                strict=True,
            )
    unique = []
    seen = {}
    for tool in body["tools"]:
        name = tool.get("name") if tool.get("type") == "function" else None
        if name and name in seen:
            if seen[name].get("parameters") != tool.get("parameters"):
                raise ValueError("Conflicting function names across namespaces")
            continue
        if name:
            seen[name] = tool
        unique.append(tool)
    body["tools"] = unique
    for item in body.get("input", []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "reasoning":
            # Codex serializes absent content as null. Grok interprets that field
            # as a compaction envelope and rejects otherwise valid reasoning.
            item.pop("content", None)
        if item.get("type") in {"function_call", "custom_tool_call"}:
            namespace = item.pop("namespace", None)
            if namespace:
                item["name"] = wire_name(namespace, item["name"])
        if item.get("type") == "custom_tool_call":
            if item.get("name") != "apply_patch":
                raise ValueError("Unsupported custom tool history")
            item.update(
                type="function_call",
                name=PATCH_FUNCTION,
                arguments=json.dumps({"input": item.pop("input", "")}),
            )
        elif item.get("type") == "custom_tool_call_output":
            item["type"] = "function_call_output"
    return body


def wire_name(namespace, name):
    if not namespace:
        return name
    digest = hashlib.sha256(f"{namespace}.{name}".encode()).hexdigest()[:24]
    return "ar_" + digest


def flattened_tools(tools, namespace=None):
    result = []
    for tool in tools:
        tool = dict(tool)
        if tool.get("type") == "namespace":
            result.extend(flattened_tools(tool.get("tools", []), tool["name"]))
        else:
            if namespace and tool.get("type") == "function":
                original = tool["name"]
                tool["name"] = wire_name(namespace, original)
                tool["description"] = f"Tool {namespace}.{original}. " + tool.get("description", "")
            result.append(tool)
    return result


def tool_names(body):
    names = {}

    def visit(tools, namespace=None):
        for tool in tools:
            if tool.get("type") == "namespace":
                visit(tool.get("tools", []), tool["name"])
            elif namespace and tool.get("type") == "function":
                names[wire_name(namespace, tool["name"])] = (namespace, tool["name"])

    visit(body.get("tools", []))
    return names


def patch_item(item, names=None):
    if item.get("type") == "function_call" and item.get("name") in (names or {}):
        item["namespace"], item["name"] = names[item["name"]]

    if item.get("type") == "function_call" and item.get("name") == PATCH_FUNCTION:
        item["type"] = "custom_tool_call"
        item["name"] = "apply_patch"
        arguments = item.pop("arguments", "")
        item["input"] = json.loads(arguments)["input"] if arguments else ""
    return item


def translate_stream(upstream, names=None):
    """Leave all Responses events intact except patch function encoding."""
    patches = set()
    for raw in upstream:
        if not raw.startswith(b"data: "):
            # Reconstruct event names from the payload after translating.
            if not raw.startswith(b"event:"):
                yield raw
            continue
        try:
            event = json.loads(raw[6:])
        except ValueError:
            yield raw
            continue
        kind = event.get("type", "")
        item = event.get("item")
        if isinstance(item, dict) and item.get("name") == PATCH_FUNCTION:
            patches.add(item["id"])
        if isinstance(item, dict):
            patch_item(item, names)
        if event.get("item_id") in patches:
            if kind == "response.function_call_arguments.delta":
                continue
            if kind == "response.function_call_arguments.done":
                event["type"] = "response.custom_tool_call_input.delta"
                event["delta"] = json.loads(event.pop("arguments"))["input"]
        for output in event.get("response", {}).get("output", []):
            patch_item(output, names)
        yield b"event: " + event.get("type", "message").encode() + b"\n"
        yield b"data: " + json.dumps(event).encode() + b"\n"
