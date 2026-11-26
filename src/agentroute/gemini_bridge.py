"""Gemini API as a Codex provider, using Google OAuth rather than an API key.

Codex speaks the OpenAI Responses API. This bridge translates a Responses
request into a Gemini ``generateContent`` call and translates the Gemini stream
back into Responses events. Gemini executes no tools itself: each
``functionCall`` it returns becomes a Responses ``function_call`` or
``custom_tool_call`` item, Codex runs the tool, and the result returns on the
next request.

Two transports reach Google directly:

``gemini``
    The public Generative Language API. The OAuth client is authorized for
    ``https://www.googleapis.com/auth/generative-language`` and usage is billed
    to the project that owns that client.

``vertex``
    Vertex AI. The credential needs the ``cloud-platform`` scope and a project
    where the account may call ``aiplatform.endpoints.predict``.

Neither transport shells out to Antigravity or any other CLI.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
GENERATIVE_SCOPE = "https://www.googleapis.com/auth/generative-language"
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
VERTEX_BASE = "https://aiplatform.googleapis.com/v1"
CONTEXT_WINDOW = 1_048_576

# Used only before the first successful discovery. ``agentroute gemini install``
# replaces these with model IDs the credential actually serves, because Google
# renames and retires models without notice.
FALLBACK_MODELS: dict[str, str] = {
    "fast": "gemini-3.5-flash-lite",
    "normal": "gemini-3.8-flash",
    "smart": "gemini-4-argon",
    "max": "gemini-4-carbon",
}

# Families that never serve a chat turn, filtered out of discovery results.
_NON_CHAT = (
    "embedding",
    "embed",
    "imagen",
    "image",
    "veo",
    "tts",
    "audio",
    "live",
    "aqa",
    "learnlm",
    "gemma",
    "transcribe",
    "genie",
)

# Ordered family preferences per tier. ``flash-lite`` must never satisfy a
# tier that needs general reasoning.
TIER_FAMILIES: dict[str, tuple[str, ...]] = {
    "fast": ("flash-lite", "flash"),
    "normal": ("flash",),
    # Argon is the efficient frontier model and Carbon the most capable one.
    "smart": ("argon", "pro", "carbon", "flash"),
    "max": ("carbon", "argon", "pro", "flash"),
}

# Every Gemini chat family is billed at one published Standard rate; these keys
# resolve in the operator's pricing table, which carries the dated changes.
PRICE_FAMILIES = ("gemini-pro", "gemini-flash", "gemini-flash-lite")


def price_family(model: str) -> str:
    """Price key for one model ID, so captured usage is never silently unpriced."""
    name = model.rsplit("/", 1)[-1]
    legacies = {
        "gemini-2.5-pro": "gemini-2.5-pro",
        "gemini-2.5-flash": "gemini-2.5-flash",
        "gemini-2.5-flash-lite": "gemini-2.5-flash-lite",
    }
    if name in legacies:
        return legacies[name]
    if "flash-lite" in name:
        return "gemini-flash-lite"
    if "flash" in name:
        return "gemini-flash"
    return "gemini-pro"


def api_base() -> str:
    """Gemini API root, overridable for staging and local protocol tests."""
    return os.environ.get("AGENTROUTE_GEMINI_API_BASE") or DEFAULT_API_BASE


def environment() -> dict[str, str]:
    """Environment that must reach the bridge, including when launchd runs it."""
    keys = (
        "AGENTROUTE_GEMINI_TRANSPORT",
        "AGENTROUTE_GEMINI_VERTEX_PROJECT",
        "AGENTROUTE_GEMINI_VERTEX_LOCATION",
        "AGENTROUTE_GEMINI_API_BASE",
        "AGENTROUTE_GEMINI_CREDENTIALS",
        "AGENTROUTE_GEMINI_TOKEN_FILE",
        "AGENTROUTE_GEMINI_MODELS",
        "AGENTROUTE_GEMINI_MODELS_CACHE",
    )
    return {key: os.environ[key] for key in keys if os.environ.get(key)}


def transport() -> str:
    """Which Google endpoint serves inference: ``gemini`` or ``vertex``."""
    return (os.environ.get("AGENTROUTE_GEMINI_TRANSPORT") or "gemini").strip().lower()


def vertex_project() -> str | None:
    return os.environ.get("AGENTROUTE_GEMINI_VERTEX_PROJECT") or None


def vertex_location() -> str:
    return os.environ.get("AGENTROUTE_GEMINI_VERTEX_LOCATION") or "global"


def model_base(model: str) -> str:
    """Endpoint prefix for one model on the configured transport."""
    quoted = urllib.parse.quote(model, safe="")
    if transport() == "vertex":
        project = vertex_project()
        if not project:
            raise CredentialError("Vertex transport needs AGENTROUTE_GEMINI_VERTEX_PROJECT")
        return (
            f"{VERTEX_BASE}/projects/{urllib.parse.quote(project, safe='')}"
            f"/locations/{urllib.parse.quote(vertex_location(), safe='')}"
            f"/publishers/google/models/{quoted}"
        )
    return f"{api_base()}/models/{quoted}"


def stream_url(model: str) -> str:
    """Streaming endpoint for one model."""
    return f"{model_base(model)}:streamGenerateContent?alt=sse"


def generate_url(model: str) -> str:
    """Non-streaming endpoint, used by ``agentroute gemini check``."""
    return f"{model_base(model)}:generateContent"


def models_url() -> str:
    """Listing endpoint for the configured transport."""
    if transport() == "vertex":
        project = vertex_project()
        if not project:
            raise CredentialError("Vertex transport needs AGENTROUTE_GEMINI_VERTEX_PROJECT")
        return (
            f"{VERTEX_BASE}/projects/{urllib.parse.quote(project, safe='')}"
            f"/locations/{urllib.parse.quote(vertex_location(), safe='')}"
            "/publishers/google/models?pageSize=200"
        )
    return f"{api_base()}/models?pageSize=1000"


def models_cache_path() -> Path:
    override = os.environ.get("AGENTROUTE_GEMINI_MODELS_CACHE")
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("AGENTROUTE_HOME") or (Path.home() / ".agentroute"))
    return home / "gemini-models.json"


def _version(model: str) -> tuple[int, int]:
    digits = model.removeprefix("gemini-").split("-", 1)[0]
    parts = digits.split(".")
    try:
        major = int(parts[0])
    except (IndexError, ValueError):
        return (0, 0)
    minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return (major, minor)


def _rank(model: str, family: str) -> tuple[int, int, int, int]:
    """Prefer newer versions, then stable over preview, then unversioned IDs."""
    major, minor = _version(model)
    stable = 0 if any(tag in model for tag in ("preview", "exp", "latest")) else 1
    unversioned = 0 if "-001" in model else 1
    return (major * 100 + minor, stable, unversioned, -len(model))


def choose_models(model_ids: Iterable[str]) -> dict[str, str]:
    """Map tiers onto the best chat models the endpoint advertises."""
    usable: list[str] = []
    for raw in model_ids:
        model = str(raw).rsplit("/", 1)[-1]
        if not model.startswith("gemini-"):
            continue
        if any(tag in model for tag in _NON_CHAT):
            continue
        if model not in usable:
            usable.append(model)
    chosen: dict[str, str] = {}
    for tier, families in TIER_FAMILIES.items():
        for family in families:
            candidates = [model for model in usable if f"-{family}" in model]
            if family == "flash" and tier != "fast":
                candidates = [model for model in candidates if "flash-lite" not in model]
            if candidates:
                chosen[tier] = max(candidates, key=lambda model: _rank(model, family))
                break
    if not chosen:
        raise CredentialError("the endpoint advertised no usable Gemini chat model")
    return chosen


def model_map() -> dict[str, str]:
    """Tier mapping in force: environment, then discovery cache, then fallback."""
    override = os.environ.get("AGENTROUTE_GEMINI_MODELS")
    if override:
        try:
            parsed = json.loads(override)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed:
            return {str(key): str(value) for key, value in parsed.items()}
    try:
        cached = json.loads(models_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = None
    if isinstance(cached, dict):
        models = cached.get("models")
        if isinstance(models, dict) and models:
            return {str(key): str(value) for key, value in models.items()}
    return dict(FALLBACK_MODELS)


def save_model_map(models: dict[str, str], available: list[str] | None = None) -> Path:
    path = models_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "transport": transport(),
                "project": vertex_project(),
                "models": models,
                "available": sorted(available or []),
                "discovered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def log(message: str) -> None:
    """Operational log line. Never includes prompts, output, or credentials."""
    print(f"agentroute-gemini: {message}", flush=True)


class CredentialError(RuntimeError):
    """The Google credential is missing, expired, or unauthorized."""


class UpstreamError(RuntimeError):
    """Gemini rejected the request."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def credential_path() -> Path:
    override = os.environ.get("AGENTROUTE_GEMINI_CREDENTIALS")
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("AGENTROUTE_HOME") or (Path.home() / ".agentroute"))
    return home / "gemini-oauth.json"


def _google_error(exc: urllib.error.HTTPError) -> str:
    detail = exc.read().decode(errors="replace")
    try:
        return str(json.loads(detail)["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return detail[:300]


def _authorized(url: str, token: str, method: str = "GET", body: dict | None = None) -> Any:
    headers = {"Authorization": f"Bearer {token}", "user-agent": "agentroute-gemini"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    return urllib.request.urlopen(request, timeout=900)


def list_models(token: str) -> list[str]:
    """Every chat-capable model name the endpoint returns for this credential."""
    try:
        with _authorized(models_url(), token) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise CredentialError(
            f"Gemini rejected the credential ({exc.code}): {_google_error(exc)}"
        ) from exc
    except (urllib.error.URLError, ValueError) as exc:
        raise CredentialError(f"cannot reach the Gemini API: {exc}") from exc
    names: list[str] = []
    for entry in body.get("models") or []:
        if not isinstance(entry, dict):
            continue
        methods = entry.get("supportedGenerationMethods") or entry.get("supportedActions") or []
        if methods and "generateContent" not in methods:
            continue
        name = entry.get("name")
        if isinstance(name, str):
            names.append(name)
    return names


def grant_scopes(access_token: str) -> str:
    """Return the scopes Google reports for a token."""
    request = urllib.request.Request(
        f"{TOKENINFO_URL}?access_token={urllib.parse.quote(access_token)}",
        headers={"user-agent": "agentroute-gemini"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise CredentialError("Google rejected the access token") from exc
    except (urllib.error.URLError, ValueError) as exc:
        raise CredentialError(f"cannot reach Google's token info endpoint: {exc}") from exc
    if body.get("error"):
        raise CredentialError("Google rejected the access token")
    return str(body.get("scope") or "")


@dataclass
class GoogleTokenCredential:
    """Reuse a Google access token that already carries ``cloud-platform``.

    Vertex AI authenticates with that scope, which installed Google tooling
    already grants, so this transport needs no second consent.
    """

    mode: str = "google-token"

    def _sources(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        env_token = os.environ.get("AGENTROUTE_GEMINI_ACCESS_TOKEN")
        if env_token:
            found.append({"access_token": env_token, "source": "environment"})
        override = os.environ.get("AGENTROUTE_GEMINI_TOKEN_FILE")
        candidates = (
            [Path(override).expanduser()]
            if override
            else [Path.home() / ".gemini" / "oauth_creds.json"]
        )
        for path in candidates:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("access_token"):
                found.append({"access_token": str(data["access_token"]), "source": str(path)})
        return found

    def token(self) -> str:
        problems: list[str] = []
        for candidate in self._sources():
            access = candidate["access_token"]
            try:
                scopes = grant_scopes(access).split()
            except CredentialError as exc:
                problems.append(f"{candidate['source']}: {exc}")
                continue
            if CLOUD_PLATFORM_SCOPE in scopes:
                return access
            problems.append(f"{candidate['source']}: token lacks the cloud-platform scope")
        raise CredentialError(
            "no Google access token with the cloud-platform scope ("
            + "; ".join(problems or ["no token found"])
            + "); set AGENTROUTE_GEMINI_ACCESS_TOKEN or AGENTROUTE_GEMINI_TOKEN_FILE"
        )

    def check(self) -> list[str]:
        token = self.token()
        list_models(token)
        return grant_scopes(token).split()


@dataclass
class GeminiCredential:
    """A Google OAuth refresh token authorized for the Gemini API."""

    mode: str = "oauth"

    def __post_init__(self) -> None:
        self._path = credential_path()
        self._access_token: str | None = None
        self._expires_at = 0.0

    def _load(self) -> dict[str, Any]:
        path = self._path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CredentialError(
                "no Gemini OAuth credential; run `agentroute gemini login`"
            ) from exc
        except (OSError, ValueError) as exc:
            raise CredentialError(f"cannot read {path}: {exc}") from exc
        for key in ("client_id", "client_secret", "refresh_token"):
            if not data.get(key):
                raise CredentialError(f"{path} is missing {key}; run `agentroute gemini login`")
        return data

    def _store(self, data: dict[str, Any]) -> None:
        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def _refresh(self, data: dict[str, Any]) -> dict[str, Any]:
        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": data["refresh_token"],
                "client_id": data["client_id"],
                "client_secret": data["client_secret"],
            }
        ).encode()
        request = urllib.request.Request(
            TOKEN_URL,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "user-agent": "agentroute-gemini",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            if "invalid_grant" in detail:
                raise CredentialError(
                    "the Gemini refresh token is no longer valid; run `agentroute gemini login`"
                ) from exc
            raise CredentialError(
                f"Gemini token refresh failed ({exc.code}): {detail[:200]}"
            ) from exc
        except (urllib.error.URLError, ValueError) as exc:
            raise CredentialError(f"cannot reach Google's token endpoint: {exc}") from exc
        if payload.get("refresh_token"):
            data["refresh_token"] = payload["refresh_token"]
        return payload

    def token(self) -> str:
        """Return a live access token, refreshing and persisting when needed."""
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        data = self._load()
        payload = self._refresh(data)
        access = payload.get("access_token")
        if not access:
            raise CredentialError("Google returned no access token")
        self._access_token = str(access)
        self._expires_at = time.time() + float(payload.get("expires_in") or 3600)
        if payload.get("refresh_token"):
            self._store(data)
        return self._access_token

    def check(self) -> list[str]:
        """Prove the credential can list models on the configured transport."""
        token = self.token()
        list_models(token)
        return grant_scopes(token).split()


def credential_for_transport():
    """Pick the credential that matches the configured endpoint."""
    if transport() == "vertex":
        return GoogleTokenCredential()
    return GeminiCredential()


def _content_text(content: Any) -> str:
    """Flatten Responses content items into plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        str(item["text"])
        for item in content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    return "\n".join(part for part in parts if part)


def _upper_types(schema: Any) -> Any:
    """Gemini's OpenAPI subset expects upper-case type names."""
    if isinstance(schema, dict):
        result = {}
        for key, value in schema.items():
            if key == "type" and isinstance(value, str):
                result[key] = value.upper()
            else:
                result[key] = _upper_types(value)
        return result
    if isinstance(schema, list):
        return [_upper_types(item) for item in schema]
    return schema


def tool_declarations(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Describe the caller's tools as Gemini function declarations.

    A freeform custom tool such as ``apply_patch`` exposes one string argument,
    which matches the text Codex sends and expects back.
    """
    declarations: list[dict[str, Any]] = []
    for raw in body.get("tools") or []:
        if not isinstance(raw, dict):
            continue
        if raw.get("type") == "custom":
            name = raw.get("name")
            if not isinstance(name, str) or not name:
                continue
            declarations.append(
                {
                    "name": name,
                    "description": str(raw.get("description") or ""),
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "input": {
                                "type": "STRING",
                                "description": (
                                    "The complete tool input exactly as the caller expects it."
                                ),
                            }
                        },
                        "required": ["input"],
                    },
                }
            )
            continue
        spec = raw.get("function") if isinstance(raw.get("function"), dict) else raw
        name = spec.get("name")
        if not isinstance(name, str) or not name:
            continue
        declaration: dict[str, Any] = {
            "name": name,
            "description": str(spec.get("description") or ""),
        }
        parameters = spec.get("parameters")
        if isinstance(parameters, dict) and parameters:
            declaration["parameters"] = _upper_types(parameters)
        declarations.append(declaration)
    return declarations


def tool_kinds(body: dict[str, Any]) -> dict[str, str]:
    """Map every declared tool name to ``custom`` or ``function``."""
    kinds: dict[str, str] = {}
    for raw in body.get("tools") or []:
        if not isinstance(raw, dict):
            continue
        spec = raw.get("function") if isinstance(raw.get("function"), dict) else raw
        name = spec.get("name")
        if isinstance(name, str) and name:
            kinds[name] = "custom" if raw.get("type") == "custom" else "function"
    return kinds


def _tool_config(choice: Any) -> dict[str, Any] | None:
    config: dict[str, Any] = {}
    if isinstance(choice, str) and choice in {"auto", "none", "required"}:
        config["mode"] = {"auto": "AUTO", "none": "NONE", "required": "ANY"}[choice]
    elif isinstance(choice, dict):
        kind = choice.get("type")
        if kind == "function" and isinstance(choice.get("name"), str):
            config["mode"] = "ANY"
            config["allowedFunctionNames"] = [choice["name"]]
        elif kind == "allowed_tools":
            names = [
                entry["name"]
                for entry in choice.get("tools") or []
                if isinstance(entry, dict) and isinstance(entry.get("name"), str)
            ]
            if names:
                config["mode"] = "ANY"
                config["allowedFunctionNames"] = names
    return {"functionCallingConfig": config} if config else None


def thinking_config(model: str, effort: str | None) -> dict[str, Any] | None:
    """Reasoning controls for the model families that document them."""
    if not effort:
        return None
    budget = {"minimal": 0, "low": 1024, "medium": 4096, "high": 8192, "xhigh": 16384}.get(effort)
    if budget is None:
        return None
    if "gemini-3" in model:
        return {"thinkingLevel": "low" if budget <= 1024 else "high"}
    if "gemini-2.5-flash" in model:
        return {"thinkingBudget": budget}
    if "gemini-2.5-pro" in model:
        # The Pro family cannot disable thinking.
        return {"thinkingBudget": max(budget, 128)}
    return None


def translate_request(body: dict[str, Any], model: str) -> dict[str, Any]:
    """Build the Gemini ``generateContent`` payload for a Responses request."""
    contents: list[dict[str, Any]] = []
    call_names: dict[str, str] = {}
    input_items = body.get("input")
    if isinstance(input_items, str):
        contents.append({"role": "user", "parts": [{"text": input_items}]})
        input_items = []
    for item in input_items or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in {"function_call", "custom_tool_call"}:
            name = str(item.get("name") or "")
            call_id = str(item.get("call_id") or "")
            if call_id:
                call_names[call_id] = name
            arguments: Any = item.get("arguments", item.get("input"))
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {"input": arguments}
            if kind == "custom_tool_call":
                arguments = {"input": item.get("input") or arguments}
            contents.append(
                {
                    "role": "model",
                    "parts": [{"functionCall": {"name": name, "args": arguments or {}}}],
                }
            )
            continue
        if kind in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(item.get("call_id") or "")
            output = item.get("output")
            text = output if isinstance(output, str) else _content_text(output)
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": call_names.get(call_id) or call_id or "tool",
                                "response": {"output": text},
                            }
                        }
                    ],
                }
            )
            continue
        text = _content_text(item.get("content"))
        if not text and isinstance(item.get("text"), str):
            text = item["text"]
        if not text.strip():
            continue
        role = "model" if str(item.get("role") or "") == "assistant" else "user"
        previous = contents[-1] if contents else None
        if previous and previous["role"] == role and "functionResponse" not in str(previous):
            previous["parts"].append({"text": text})
        else:
            contents.append({"role": role, "parts": [{"text": text}]})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": "Continue."}]}]

    payload: dict[str, Any] = {"contents": contents}
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        payload["systemInstruction"] = {"parts": [{"text": instructions.strip()}]}
    declarations = tool_declarations(body)
    if declarations:
        payload["tools"] = [{"functionDeclarations": declarations}]
        config = _tool_config(body.get("tool_choice"))
        if config:
            payload["toolConfig"] = config
    generation: dict[str, Any] = {}
    limit = body.get("max_output_tokens")
    if isinstance(limit, int) and limit > 0:
        generation["maxOutputTokens"] = limit
    reasoning = body.get("reasoning")
    effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
    thinking = thinking_config(model, str(effort) if effort else None)
    if thinking:
        generation["thinkingConfig"] = thinking
    text_config = body.get("text")
    schema = (
        (text_config.get("format") or {}).get("schema") if isinstance(text_config, dict) else None
    )
    if isinstance(schema, dict) and schema:
        generation["responseMimeType"] = "application/json"
        generation["responseSchema"] = _upper_types(schema)
    if generation:
        payload["generationConfig"] = generation
    return payload


def response_text(body: dict[str, Any]) -> str:
    """Extract the assistant text from a non-streaming Gemini response."""
    for candidate in body.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            if part.get("thought") is True:
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                return text
    return ""


def usage_from(metadata: dict[str, Any] | None) -> dict[str, int]:
    """Map Gemini usage metadata onto Responses usage fields."""
    if not isinstance(metadata, dict):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    prompt = int(metadata.get("promptTokenCount") or 0)
    candidates = int(metadata.get("candidatesTokenCount") or 0)
    total = int(metadata.get("totalTokenCount") or (prompt + candidates))
    return {"input_tokens": prompt, "output_tokens": candidates, "total_tokens": total}


def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def iter_sse(response: Any) -> Iterator[dict[str, Any]]:
    """Yield decoded JSON objects from a Gemini ``alt=sse`` response."""
    for raw in response:
        line = raw.decode(errors="replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except ValueError:
            continue


class ResponsesStream:
    """Turn Gemini stream chunks into OpenAI Responses events."""

    def __init__(self, model: str, tools: dict[str, str]) -> None:
        self.model = model
        self.tools = tools
        self.response_id = f"resp_{uuid.uuid4().hex}"
        self.text: list[str] = []
        self.items: list[dict[str, Any]] = []
        self.usage = usage_from(None)
        self.text_item_id: str | None = None
        self.started = False
        self.finished = False

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

    def feed(self, chunk: dict[str, Any]) -> Iterator[bytes]:
        if not self.started:
            yield self._created()
        if chunk.get("usageMetadata"):
            self.usage = usage_from(chunk["usageMetadata"])
        for candidate in chunk.get("candidates") or []:
            for part in (candidate.get("content") or {}).get("parts") or []:
                # Gemini 3 returns its reasoning as text parts marked
                # ``thought``. Those are not assistant output.
                if part.get("thought") is True:
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    yield from self._text(text)
                call = part.get("functionCall")
                if isinstance(call, dict) and call.get("name"):
                    yield from self._call(call)
            if candidate.get("finishReason"):
                self.finished = True

    def _text(self, delta: str) -> Iterator[bytes]:
        if self.text_item_id is None:
            self.text_item_id = f"msg_{uuid.uuid4().hex}"
            yield sse(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": len(self.items),
                    "item": {
                        "type": "message",
                        "id": self.text_item_id,
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": ""}],
                    },
                },
            )
            yield sse(
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "item_id": self.text_item_id,
                    "output_index": len(self.items),
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                },
            )
        self.text.append(delta)
        yield sse(
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "item_id": self.text_item_id,
                "output_index": len(self.items),
                "content_index": 0,
                "delta": delta,
            },
        )

    def _close_text(self) -> Iterator[bytes]:
        if self.text_item_id is None:
            return
        text = "".join(self.text)
        index = len(self.items)
        item = {
            "type": "message",
            "id": self.text_item_id,
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
        self.items.append(item)
        yield sse(
            "response.output_text.done",
            {
                "type": "response.output_text.done",
                "item_id": self.text_item_id,
                "output_index": index,
                "content_index": 0,
                "text": text,
            },
        )
        yield sse(
            "response.output_item.done",
            {"type": "response.output_item.done", "output_index": index, "item": item},
        )
        self.text_item_id = None
        self.text = []

    def _call(self, call: dict[str, Any]) -> Iterator[bytes]:
        yield from self._close_text()
        name = str(call["name"])
        kind = self.tools.get(name, "function")
        arguments = call.get("args") if isinstance(call.get("args"), dict) else {}
        index = len(self.items)
        call_id = f"call_{uuid.uuid4().hex}"
        if kind == "custom":
            raw = arguments.get("input")
            text = raw if isinstance(raw, str) else json.dumps(arguments)
            item = {
                "type": "custom_tool_call",
                "id": f"ctc_{uuid.uuid4().hex}",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "input": text,
            }
            yield sse(
                "response.output_item.added",
                {"type": "response.output_item.added", "output_index": index, "item": item},
            )
            yield sse(
                "response.custom_tool_call_input.delta",
                {
                    "type": "response.custom_tool_call_input.delta",
                    "item_id": item["id"],
                    "call_id": call_id,
                    "output_index": index,
                    "delta": text,
                },
            )
            yield sse(
                "response.custom_tool_call_input.done",
                {
                    "type": "response.custom_tool_call_input.done",
                    "item_id": item["id"],
                    "call_id": call_id,
                    "output_index": index,
                    "input": text,
                },
            )
        else:
            encoded = json.dumps(arguments, separators=(",", ":"))
            item = {
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex}",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "arguments": encoded,
            }
            yield sse(
                "response.output_item.added",
                {"type": "response.output_item.added", "output_index": index, "item": item},
            )
            yield sse(
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": item["id"],
                    "output_index": index,
                    "delta": encoded,
                },
            )
            yield sse(
                "response.function_call_arguments.done",
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": item["id"],
                    "output_index": index,
                    "arguments": encoded,
                },
            )
        self.items.append(item)
        yield sse(
            "response.output_item.done",
            {"type": "response.output_item.done", "output_index": index, "item": item},
        )

    def result(self) -> dict[str, Any]:
        return {
            "id": self.response_id,
            "object": "response",
            "status": "completed",
            "model": self.model,
            "output": self.items,
            "usage": self.usage,
        }

    def complete(self) -> Iterator[bytes]:
        yield from self._close_text()
        if not self.started:
            yield self._created()
        yield sse(
            "response.completed",
            {"type": "response.completed", "response": self.result()},
        )


def open_stream(payload: dict[str, Any], model: str, token: str, *, timeout: float = 900.0) -> Any:
    """Open Gemini's streaming endpoint for one turn."""
    try:
        return _authorized(stream_url(model), token, method="POST", body=payload)
    except urllib.error.HTTPError as exc:
        raise UpstreamError(exc.code, _google_error(exc)) from exc
    except urllib.error.URLError as exc:
        raise UpstreamError(502, f"cannot reach the Gemini API: {exc.reason}") from exc


def probe(model: str) -> str:
    """One minimal inference call, proving the credential serves the model."""
    token = credential_for_transport().token()
    body = {
        "contents": [{"role": "user", "parts": [{"text": "Reply with OK."}]}],
        "generationConfig": {"maxOutputTokens": 32},
    }
    try:
        with _authorized(generate_url(model), token, method="POST", body=body) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise CredentialError(
            f"Gemini rejected an inference call ({exc.code}): {_google_error(exc)}"
        ) from exc
    except (urllib.error.URLError, ValueError) as exc:
        raise CredentialError(f"cannot reach the Gemini API: {exc}") from exc
    text = response_text(payload)
    if not text:
        raise CredentialError(f"Gemini returned no text: {json.dumps(payload)[:300]}")
    return text


class GeminiHandler(BaseHTTPRequestHandler):
    """Serve Codex's Responses endpoint over the Gemini API."""

    protocol_version = "HTTP/1.1"
    credential_factory = staticmethod(credential_for_transport)

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
            try:
                self.credential_factory().check()
                status = "ok"
            except CredentialError as exc:
                status = str(exc)
            self._json(
                200,
                {
                    "provider": "gemini",
                    "transport": transport(),
                    "project": vertex_project() if transport() == "vertex" else None,
                    "auth": "google-oauth",
                    "credential_status": status,
                    "models": model_map(),
                    "context_window": CONTEXT_WINDOW,
                    "tool_calling": "full",
                    "tool_calling_mode": "native_function_calling",
                },
            )
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if not self.path.startswith("/v1/responses"):
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
        model = str(body.get("model") or model_map()["normal"])
        try:
            token = self.credential_factory().token()
            upstream = open_stream(translate_request(body, model), model, token)
        except CredentialError as exc:
            self._json(503, {"error": str(exc)})
            return
        except UpstreamError as exc:
            self._json(exc.status, {"error": str(exc)})
            return
        stream = ResponsesStream(model, tool_kinds(body))
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                with upstream:
                    for chunk in iter_sse(upstream):
                        for event in stream.feed(chunk):
                            self.wfile.write(event)
            except (OSError, ValueError) as exc:
                log(f"stream ended early: {type(exc).__name__}")
            for event in stream.complete():
                self.wfile.write(event)
            self.close_connection = True
            return
        with upstream:
            for chunk in iter_sse(upstream):
                for _event in stream.feed(chunk):
                    pass
        list(stream.complete())
        self._json(200, stream.result())


class GeminiServer(ThreadingHTTPServer):
    daemon_threads = True


def serve(host: str = "127.0.0.1", port: int = 8817) -> None:
    server = GeminiServer((host, port), GeminiHandler)
    log(f"listening on http://{host}:{port}/v1 ({transport()} transport)")
    server.serve_forever()
