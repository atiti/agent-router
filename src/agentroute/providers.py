from __future__ import annotations

import os
import re
import shlex
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .config import AppConfig

START_MARKER = "# >>> agentroute model providers >>>"
END_MARKER = "# <<< agentroute model providers <<<"
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _provider_block(config: AppConfig) -> str:
    lines = [START_MARKER]
    for name, backend in sorted(config.backends.items()):
        if name == "gpt" or not backend.enabled:
            continue
        if not backend.base_url:
            continue
        lines.extend(
            [
                f"[model_providers.{backend.codex_provider}]",
                f"name = {_toml_string(backend.display_name)}",
                f"base_url = {_toml_string(backend.base_url.rstrip('/'))}",
                'wire_api = "responses"',
                "requires_openai_auth = false",
                "supports_websockets = false",
            ]
        )
        review_target = backend.review_model or backend.tiers["smart"].model
        lines.append(f"approval_review_model = {_toml_string(review_target)}")
        if backend.tool_compatibility == "functions_and_apply_patch":
            lines.append(
                "tool_compatibility = "
                f"{_toml_string(backend.tool_compatibility)}"
            )
        if backend.api_key_env:
            if backend.api_key_header.lower() == "authorization":
                lines.append(f"env_key = {_toml_string(backend.api_key_env)}")
            else:
                header = _toml_string(backend.api_key_header)
                env = _toml_string(backend.api_key_env)
                lines.append(f"env_http_headers = {{ {header} = {env} }}")
        lines.append("")
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def sync_codex_providers(
    config: AppConfig,
    path: Path | None = None,
    *,
    backup: bool = True,
) -> tuple[Path, Path | None]:
    """Replace only AgentRoute's marked provider block in Codex configuration."""
    path = path or Path.home() / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    start = current.find(START_MARKER)
    end = current.find(END_MARKER)
    if (start == -1) != (end == -1) or (start != -1 and end < start):
        raise ValueError(f"malformed AgentRoute provider block in {path}")
    if start != -1:
        original = current
        end += len(END_MARKER)
        while end < len(original) and original[end] in "\r\n":
            end += 1
        prefix = original[:start].rstrip()
        suffix = original[end:].lstrip("\r\n")
        current = prefix + ("\n\n" + suffix if prefix and suffix else suffix)
    rendered = current.rstrip() + "\n\n" + _provider_block(config)
    backup_path: Path | None = None
    if path.exists() and path.read_text(encoding="utf-8") != rendered and backup:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = path.with_name(f"{path.name}.agentroute-backup-{timestamp}")
        shutil.copy2(path, backup_path)
    path.write_text(rendered, encoding="utf-8")
    return path, backup_path


def backend_readiness(config: AppConfig, name: str) -> tuple[bool, list[str]]:
    backend = config.backends[name]
    problems: list[str] = []
    if not backend.enabled:
        problems.append("disabled")
    if name != "gpt" and not backend.base_url:
        problems.append("missing base URL")
    stored = stored_credential_names()
    if (
        backend.api_key_env
        and not os.environ.get(backend.api_key_env)
        and backend.api_key_env not in stored
    ):
        problems.append(f"missing {backend.api_key_env}")
    return not problems, problems


def credentials_path() -> Path:
    override = os.environ.get("AGENTROUTE_CREDENTIALS_FILE")
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("AGENTROUTE_HOME", Path.home() / ".agentroute"))
    return home / "backend-credentials.env"


def stored_credential_names(path: Path | None = None) -> set[str]:
    path = path or credentials_path()
    if not path.exists():
        return set()
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^export ([A-Z_][A-Z0-9_]*)=", line)
        if match:
            names.add(match.group(1))
    return names


def import_backend_credential(
    config: AppConfig,
    backend_name: str,
    source_env: str,
    path: Path | None = None,
) -> tuple[Path, str]:
    """Persist one credential in an owner-only shell file without printing it."""
    backend = config.backends[backend_name]
    target_env = backend.api_key_env
    if not target_env or not ENV_NAME.fullmatch(target_env):
        raise ValueError(f"backend {backend_name} has no valid credential environment variable")
    if not ENV_NAME.fullmatch(source_env):
        raise ValueError("source environment variable name is invalid")
    value = os.environ.get(source_env)
    if not value:
        raise ValueError(f"source environment variable is empty: {source_env}")
    path = path or credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"refusing to write credential symlink: {path}")
    retained = []
    if path.exists():
        retained = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(f"export {target_env}=")
        ]
    retained.append(f"export {target_env}={shlex.quote(value)}")
    rendered = "\n".join(retained) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            temporary.chmod(0o600)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    return path, target_env
