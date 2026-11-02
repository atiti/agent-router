"""Loopback Responses proxy for Grok CLI subscriptions or the public xAI API.

The CLI alone owns OAuth refresh and credential writes. No tokens enter AgentRoute
configuration, command arguments, logs, or provider catalogs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shlex
import stat
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .config import ExecutionBackendConfig, ModelCapabilities, ModelTarget, agentroute_home

UPSTREAM = "https://cli-chat-proxy.grok.com/v1"
API_UPSTREAM = "https://api.x.ai/v1"
DEFAULT_PORT = 8092
API_PORT = 8093
MAX_BODY = 50 * 1024 * 1024
MODELS = ("grok-4.7-build-fast", "grok-4.6", "grok-4.7", "grok-4.5")
API_MODELS = tuple(model for model in MODELS if model != "grok-4.7-build-fast")
BRIDGE_KEY_ENV = "AGENTROUTE_GROK_BRIDGE_KEY"
BRIDGE_KEY_PREFIX = "ar-grok-"


class GrokCredential:
    mode = "subscription"
    upstream = UPSTREAM
    models = MODELS

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
            and isinstance(credential.get("key"), str)
            and credential["key"]
        ]
        if len(candidates) == 1:
            return candidates[0]
        raise ValueError("No Grok subscription login; run grok login")

    def current(self):
        """Read one login so authorization and quota attribution stay together."""
        with self.lock:
            credential = self._read()
            expiry = self._expiry(credential)
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
                expiry = self._expiry(credential)
                if expiry <= datetime.now(timezone.utc):
                    raise ValueError("Grok login expired; run grok login")
            return credential

    @staticmethod
    def _expiry(credential):
        value = credential.get("expires_at")
        if not isinstance(value, str):
            raise ValueError("Invalid Grok login expiry; run grok login")
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            raise ValueError("Invalid Grok login expiry; run grok login")
        return expiry

    def token(self):
        return self.current()["key"]

    def version(self):
        result = subprocess.run(
            [self.binary, "--version"], capture_output=True, text=True, timeout=10, check=True
        )
        match = re.search(r"\b(\d+\.\d+\.\d+)\b", result.stdout)
        if not match:
            raise ValueError("Cannot determine installed Grok CLI version")
        return match[1]


class MissingCredential(ValueError):
    pass


def api_credentials_path():
    override = os.environ.get("AGENTROUTE_GROK_API_CREDENTIALS_FILE")
    if override:
        return Path(override).expanduser()
    return agentroute_home() / "grok-api-credentials.env"


def read_managed_credential(name, path=None):
    """Read one owner-only credential without evaluating shell-file contents."""
    from .providers import credentials_path

    descriptor = os.open(path or credentials_path(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, encoding="utf-8") as handle:
        metadata = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > 65536
        ):
            raise ValueError("Credential file must be an owner-only regular file")
        candidates = []
        for line in handle.read(65536).splitlines():
            if line.startswith(f"export {name}="):
                parts = shlex.split(line)
                if len(parts) != 2 or parts[0] != "export":
                    raise ValueError("Invalid managed credential")
                candidates.append(parts[1].removeprefix(name + "="))
        if not candidates:
            raise MissingCredential("Managed credential is missing")
        if len(candidates) != 1:
            raise ValueError("Duplicate managed credential")
        key = candidates[0]
    if not key or any(not 33 <= ord(char) <= 126 for char in key):
        raise ValueError("Invalid managed credential")
    return key


class GrokApiCredential:
    """Keep public API and local-client credentials at their separate boundaries."""

    mode = "api-key"
    upstream = API_UPSTREAM
    models = API_MODELS

    def _read(self):
        from .providers import credentials_path

        if api_credentials_path().resolve() == credentials_path().resolve():
            raise ValueError("Upstream and client credential files must be separate")
        return {"key": read_managed_credential("XAI_API_KEY", api_credentials_path())}

    def current(self):
        return self._read()

    def token(self):
        return self.current()["key"]

    def version(self):
        return None  # API keys do not require a Grok CLI installation or OAuth refresh.

    def client_key(self):
        return read_managed_credential(BRIDGE_KEY_ENV)

    def health_proof(self, nonce, instance="", port=0):
        # Prove the responding bridge owns both intended credentials without exposing either.
        return hmac.new(
            self.client_key().encode(),
            f"{nonce}\0{instance}\0{port}\0".encode() + self.token().encode(),
            hashlib.sha256,
        ).hexdigest()


def ensure_backend(config, port=None, credential_mode="subscription"):
    if credential_mode not in {"subscription", "api-key"}:
        raise ValueError("Unknown Grok credential mode")
    api_key = credential_mode == "api-key"
    name = "grok-api" if api_key else "grok"
    if port is None:
        port = API_PORT if api_key else DEFAULT_PORT
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    other_name = "grok" if api_key else "grok-api"
    other = config.backends.get(other_name)
    if other is not None and urlsplit(other.base_url).port == port:
        raise ValueError(f"Port {port} is already configured for {other_name}; use a separate port")
    if name not in config.backends:
        config.backends[name] = ExecutionBackendConfig(
            enabled=True,
            codex_provider="agentroute-grok-api" if api_key else "agentroute-grok",
            display_name="Grok API" if api_key else "Grok subscription (Grok CLI)",
            base_url=f"http://127.0.0.1:{port}/v1",
            tool_compatibility="functions_and_apply_patch_preserve_reasoning",
            review_model="grok-4.7" if api_key else MODELS[0],
            tiers={
                tier: ModelTarget(
                    model="grok-4.7" if api_key else model,
                    reasoning_effort=effort,
                    context_window=500000 if api_key else 256000,
                )
                for tier, model, effort in (
                    ("fast", MODELS[0], "low"),
                    ("normal", "grok-4.6", "medium"),
                    ("smart", "grok-4.7", "high"),
                    ("max", "grok-4.7", "xhigh"),
                )
            },
        )
    else:
        config.backends[name].enabled = True
        config.backends[name].base_url = f"http://127.0.0.1:{port}/v1"
    backend = config.backends[name]
    # Clients authenticate locally with a separate key; xAI credentials stay upstream.
    backend.api_key_env = BRIDGE_KEY_ENV if api_key else None
    backend.api_key_header = "authorization"
    for model in API_MODELS if api_key else MODELS:
        config.capabilities.models.setdefault(
            model,
            ModelCapabilities(
                tool_calling="apply_patch_only",
                reasoning=True,
                vision=True,
                context_window=500000 if api_key and model == "grok-4.7" else 256000,
            ),
        )


def request_headers(credential, version, model, *, login=None):
    headers = {
        "Authorization": "Bearer " + (login["key"] if login is not None else credential.token()),
        "Content-Type": "application/json",
    }
    if getattr(credential, "mode", "subscription") == "api-key":
        return headers
    return {
        **headers,
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

    def __init__(self, port, credential, instance=""):
        self.credential = credential
        self.instance = instance
        try:
            self.api_client_key = read_managed_credential(BRIDGE_KEY_ENV)
        except (OSError, ValueError):
            self.api_client_key = None
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
        url = urlsplit(self.path)
        if url.path == "/healthz":
            proof = {}
            if url.query and getattr(self.server.credential, "mode", "subscription") == "api-key":
                nonce = parse_qs(url.query).get("nonce", [""])[0]
                if not re.fullmatch(r"[0-9a-f]{64}", nonce):
                    self._json(400, {"error": "Invalid health challenge"})
                    return
                try:
                    proof["credential_proof"] = self.server.credential.health_proof(
                        nonce, self.server.instance, self.server.server_port
                    )
                except (OSError, ValueError):
                    self._json(503, {"error": "Grok credentials unavailable"})
                    return
            self._json(
                200,
                {
                    "provider": "grok",
                    "version": __version__,
                    "credential_mode": getattr(self.server.credential, "mode", "subscription"),
                    "instance": self.server.instance,
                    **proof,
                },
            )
        elif self.path == "/usage":
            from .grok_usage import fetch_usage, read_snapshot

            if getattr(self.server.credential, "mode", "subscription") == "api-key":
                # Public API billing is not Grok CLI subscription allowance.
                self._json(
                    200,
                    dict(
                        read_snapshot(self.server.credential),
                        credential_mode="api-key",
                        billing="unavailable",
                    ),
                )
            else:
                self._json(200, fetch_usage(self.server.credential, self.server.cli_version))
        elif self.path == "/v1/models":
            models = getattr(self.server.credential, "models", MODELS)
            self._json(200, {"object": "list", "data": [{"id": m} for m in models]})
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
        if getattr(self.server.credential, "mode", "subscription") == "api-key":
            try:
                expected = "Bearer " + self.server.credential.client_key()
            except (OSError, ValueError):
                self._json(
                    503, {"error": "Local bridge credential unavailable; reinstall the bridge"}
                )
                return
            supplied = self.headers.get("Authorization", "")
            if (
                not expected.startswith("Bearer " + BRIDGE_KEY_PREFIX)
                or not supplied.isascii()
                or not hmac.compare_digest(supplied, expected)
            ):
                self._json(401, {"error": "Local bridge authentication required"})
                return
        elif self.headers.get("Authorization"):
            api_client = (
                "Bearer " + self.server.api_client_key if self.server.api_client_key else ""
            )
            supplied = self.headers["Authorization"]
            if supplied.startswith("Bearer " + BRIDGE_KEY_PREFIX) or (
                api_client and supplied.isascii() and hmac.compare_digest(supplied, api_client)
            ):
                # A loaded API client must not spend subscription allowance after a switch.
                self._json(401, {"error": "Client credential mode does not match the Grok service"})
                return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(size))
            models = getattr(self.server.credential, "models", MODELS)
            if not isinstance(body, dict) or body.get("model") not in models:
                raise ValueError("Unknown Grok model")
            body["stream"] = True
            body["store"] = False
            names = tool_names(body)
            body = translate_request(body)
            from .grok_usage import account_key

            login = self.server.credential.current()
            headers = request_headers(
                self.server.credential, self.server.cli_version, body["model"], login=login
            )
            account = account_key(self.server.credential, login=login)
            request = urllib.request.Request(
                getattr(self.server.credential, "upstream", UPSTREAM)
                + self.path.removeprefix("/v1"),
                data=json.dumps(body).encode(),
                headers=headers,
            )
            upstream = open_upstream(request, timeout=300)
        except (ValueError, KeyError, TypeError):
            self._json(400, {"error": "Invalid request or Grok credential"})
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
            self._json(502, {"error": "Grok unavailable; check the configured credential mode"})
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

    # Bound unrequested generations: Grok otherwise may stream 128k output tokens.
    # Preserve explicit client limits, including lower limits.
    body.setdefault("max_output_tokens", 16384)

    body["tools"] = flattened_tools(body.get("tools", []))
    choice = body.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") == "custom":
        if choice.get("name") != "apply_patch":
            raise ValueError("Unsupported custom tool choice")
        body["tool_choice"] = {"type": "function", "name": PATCH_FUNCTION}
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
    if not unique:
        # Codex's local compactor sends auto with an empty tool list.
        body.pop("tool_choice", None)
        body.pop("parallel_tool_calls", None)
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


def sequential_stream(upstream):
    """Keep each output item contiguous for Codex's single active-item decoder.

    Grok can finish a tool call while a message is still streaming. Forwarding
    that done event clears Codex's active message; its next text delta can panic
    the sampling task and leave the turn open with only reviewers running.
    Stream the first item immediately, buffering overlapping items until it ends.
    """
    pending = {}
    finished = set()
    for raw in upstream:
        if not raw.startswith(b"data: "):
            if not raw.startswith(b"event:"):
                yield raw
            continue
        try:
            event = json.loads(raw[6:])
        except ValueError:
            yield raw
            continue
        kind = event.get("type", "")
        item = event.get("item") or {}
        key = event.get("output_index", event.get("item_id", item.get("id")))
        if key is None:
            yield raw
            continue
        pending.setdefault(key, []).append(raw)
        if kind == "response.output_item.done":
            finished.add(key)
        while pending:
            first = next(iter(pending))
            yield from pending[first]
            pending[first].clear()
            if first not in finished:
                break
            del pending[first]
            finished.remove(first)


def translate_stream(upstream, names=None):
    """Leave all Responses events intact except patch function encoding."""
    patches = set()
    for raw in sequential_stream(upstream):
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
        # Buffered items no longer share the upstream blank-line timing.
        yield b"data: " + json.dumps(event).encode() + b"\n\n"
