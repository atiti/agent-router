from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

from .config import (
    AppConfig,
    ExecutionBackendConfig,
    ModelTarget,
    agentroute_home,
    codex_home,
    model_capabilities,
)

START_MARKER = "# >>> agentroute model providers >>>"
END_MARKER = "# <<< agentroute model providers <<<"
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# The tiers the local Claude bridge serves. Kept here so `bridge install` and the
# documented `backend-add` invocation cannot drift apart.
CLAUDE_BRIDGE_TIERS: dict[str, str] = {
    "fast": "claude-haiku-5-5",
    "normal": "claude-sonnet-5-5",
    "smart": "claude-sonnet-5-5",
    "max": "claude-opus-5-5",
}
SIGNED_REASONING_COMPATIBILITY_MIN_RUNTIME = 54


def runtime_supports_signed_reasoning_compatibility(build_id: str | None = None) -> bool:
    """Check the installed runtime receipt before writing its fork-only enum."""
    if build_id is None:
        build_id = os.environ.get("AGENTROUTE_RUNTIME_BUILD_ID")
    if build_id is None:
        try:
            build_id = (agentroute_home() / "build-id").read_text(encoding="utf-8").strip()
        except OSError:
            return False
    match = re.search(r"(?:^|-)provider-routing-v(\d+)$", build_id)
    return bool(match and int(match.group(1)) >= SIGNED_REASONING_COMPATIBILITY_MIN_RUNTIME)


def ensure_claude_bridge_backend(
    config: AppConfig, port: int, host: str = "127.0.0.1", *, update_models: bool = False
) -> bool:
    """Register Claude and its explicit Fable route without changing default routing.

    Existing tier choices survive reinstall unless ``update_models`` is requested.
    """
    base_url = f"http://{host}:{port}/v1"
    created = "claude" not in config.backends
    tiers = {
        tier: ModelTarget(
            model=model,
            reasoning_effort={
                "fast": "low",
                "normal": "medium",
                "smart": "high",
                "max": "high",
            }[tier],
        )
        for tier, model in CLAUDE_BRIDGE_TIERS.items()
    }
    if created:
        config.backends["claude"] = ExecutionBackendConfig(
            enabled=True,
            codex_provider="agentroute-claude",
            display_name="Claude subscription (AgentRoute bridge)",
            base_url=base_url,
            tool_compatibility="functions_and_apply_patch_preserve_reasoning",
            review_model=CLAUDE_BRIDGE_TIERS["fast"],
            tiers=tiers,
        )
    else:
        config.backends["claude"].base_url = base_url
        if config.backends["claude"].tool_compatibility in {
            None,
            "functions_and_apply_patch",
        }:
            config.backends[
                "claude"
            ].tool_compatibility = "functions_and_apply_patch_preserve_reasoning"
        if update_models:
            config.backends["claude"].tiers = tiers
    # Separate backend: never added to backend_by_tier or a fallback chain.
    # Every tier uses Fable so @fable cannot accidentally select another model.
    if "fable" not in config.backends:
        config.backends["fable"] = ExecutionBackendConfig(
            enabled=True,
            codex_provider="agentroute-claude-fable",
            display_name="Claude Fable (explicit opt-in)",
            base_url=base_url,
            tool_compatibility="functions_and_apply_patch_preserve_reasoning",
            review_model=CLAUDE_BRIDGE_TIERS["fast"],
            tiers={
                tier: ModelTarget(model="claude-fable-5-1", reasoning_effort="high")
                for tier in CLAUDE_BRIDGE_TIERS
            },
        )
    else:
        config.backends["fable"].base_url = base_url
        if config.backends["fable"].tool_compatibility in {
            None,
            "functions_and_apply_patch",
        }:
            config.backends[
                "fable"
            ].tool_compatibility = "functions_and_apply_patch_preserve_reasoning"
    config.policy.backend_risk_floors.setdefault(
        "claude", {flag: "max" for flag in ("auth", "security", "database_migration", "production")}
    )
    return created


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def effective_review_model(config: AppConfig, backend_name: str) -> str:
    """Return the bounded automatic-review model actually used by a backend."""
    if backend_name == "gpt":
        return "codex-auto-review"
    backend = config.backends[backend_name]
    return backend.review_model or backend.tiers["fast"].model


def _runtime_build_id() -> str | None:
    build_id = os.environ.get("AGENTROUTE_RUNTIME_BUILD_ID")
    if build_id:
        return build_id.strip()
    try:
        return (agentroute_home() / "build-id").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _provider_context_windows(config: AppConfig, backend_name: str) -> dict[str, int]:
    """Keep provider model limits separate from shared account catalog entries."""
    if backend_name == "gpt":
        return {}
    match = re.search(r"provider-routing-v(\d+)$", _runtime_build_id() or "")
    if not match or int(match.group(1)) < 59:
        return {}
    return {
        target.model: window
        for target in config.backends[backend_name].tiers.values()
        if (window := model_capabilities(config, backend_name, target.model).context_window)
    }


def _installed_codex_catalog(source: Path | None = None) -> dict | None:
    """Read the bundled catalog from the exact invoking runtime revision."""
    build_id = _runtime_build_id()
    if not build_id:
        return None
    match = re.fullmatch(r"([0-9a-f]{40})-provider-routing-v[0-9]+", build_id)
    if match is None:
        return None
    commit = match.group(1)
    candidates = [source] if source else []
    if configured_source := os.environ.get("AGENTROUTE_CODEX_SOURCE"):
        candidates.append(Path(configured_source))
    candidates.extend((agentroute_home() / "src/codex-stack", agentroute_home() / "src/codex"))
    for candidate in candidates:
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(candidate),
                    "show",
                    f"{commit}:codex-rs/models-manager/models.json",
                ],
                capture_output=True,
                check=True,
                text=True,
            )
            catalog = json.loads(result.stdout)
            if isinstance(catalog, dict) and isinstance(catalog.get("models"), list):
                return catalog
        except (OSError, subprocess.CalledProcessError, ValueError):
            continue
    return None


def _is_managed_model_catalog_path(path: str, config_path: Path) -> bool:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = config_path.parent / candidate
    if candidate in {
        agentroute_home() / "models" / "codex-catalog.json",
        config_path.with_suffix(".agentroute-models.json"),
    }:
        return True
    return (
        candidate.parent == config_path.parent
        and candidate.name.startswith(f"{config_path.stem}.agentroute-models-")
        and candidate.suffix == ".json"
    )


def _launch_model_catalog_path(config_path: Path) -> Path:
    build_id = _runtime_build_id() or ""
    match = re.fullmatch(r"([0-9a-f]{40})-provider-routing-v([0-9]+)", build_id)
    key = f"{match.group(1)[:12]}-v{match.group(2)}" if match else "unversioned"
    return config_path.with_name(f"{config_path.stem}.agentroute-models-{key}.json")


def _refresh_context_limits(
    catalog: dict, config_path: Path, required_models: set[str] | None = None
) -> None:
    """Refresh limits and admit newly configured models from a recent account catalog."""
    try:
        cached = json.loads((config_path.parent / "models_cache.json").read_text())
        fetched = datetime.fromisoformat(cached["fetched_at"].replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - fetched
        if not timedelta(minutes=-5) <= age <= timedelta(days=1):
            return
        models = cached["models"]
        if not isinstance(models, list):
            return
    except (OSError, ValueError, TypeError, KeyError):
        return
    by_slug = {
        m["slug"]: m for m in models
        if isinstance(m, dict) and isinstance(m.get("slug"), str)
    }
    installed_slugs = {model.get("slug") for model in catalog["models"]}
    for slug in sorted((required_models or set()) - installed_slugs):
        fresh = by_slug.get(slug, {})
        window = fresh.get("context_window")
        maximum = fresh.get("max_context_window")
        if type(window) is int and type(maximum) is int and 0 < window <= maximum:
            # New releases may precede the pinned runtime catalog. Use their actual
            # account descriptor rather than guessing another model's tool protocol.
            catalog["models"].append(copy.deepcopy(fresh))
            catalog.setdefault("agentroute_account_models", []).append(slug)
    for model in catalog["models"]:
        fresh = by_slug.get(model.get("slug"), {})
        window = fresh.get("context_window")
        maximum = model.get("max_context_window")
        if type(window) is not int or window <= 0:
            continue
        if type(maximum) is not int or not 0 < window <= maximum:
            continue
        model["context_window"] = window
        limit = fresh.get("auto_compact_token_limit")
        if "auto_compact_token_limit" in fresh and (
            limit is None or (type(limit) is int and 0 < limit <= window)
        ):
            model["auto_compact_token_limit"] = limit


def _write_model_catalog(
    config: AppConfig, source: Path | None = None, *, config_path: Path
) -> Path | None:
    # The local provider descriptor must be present in the same catalog Codex
    # uses for routing. Otherwise unknown Claude slugs inherit GPT fallback limits.
    from .claude_bridge import catalog_from_config, model_catalog

    claude = config.backends.get("claude")
    claude_models = (
        model_catalog(catalog_from_config(config))["models"]
        if claude is not None and claude.enabled
        else []
    )
    aliases = {
        alias: canonical
        for alias, canonical in config.pricing.aliases.items()
        if alias != canonical
        and any(
            backend.enabled and any(target.model == alias for target in backend.tiers.values())
            for backend in config.backends.values()
        )
    }
    routed_backends = [
        (name, backend)
        for name, backend in config.backends.items()
        if backend.enabled and name != "claude"
    ]
    account_slugs = {
        target.model for target in config.backends["gpt"].tiers.values()
    } if "gpt" in config.backends else set()
    has_routed_limits = any(
        capabilities.context_window is not None
        for name, backend in routed_backends
        for target in backend.tiers.values()
        if (capabilities := model_capabilities(config, name, target.model))
    )
    if not aliases and not claude_models and not has_routed_limits:
        return None
    catalog = _installed_codex_catalog(source)
    if catalog is None:
        if aliases:
            raise ValueError("cannot read the installed Codex model catalog for routed aliases")
        return None  # An uninstalled runtime can still fetch the bridge's catalog.
    required_models = account_slugs | set(aliases.values())
    _refresh_context_limits(catalog, config_path, required_models)
    # Once admitted from the account catalog, keep new descriptors in this
    # runtime's generated sidecar even if its original account sample ages out.
    # Existing bundled models still use only the runtime's harness metadata.
    try:
        previous = json.loads(_launch_model_catalog_path(config_path).read_text())
        present = {model.get("slug") for model in catalog["models"]}
        admitted = set(previous.get("agentroute_account_models", []))
        for model in previous.get("models", []):
            if (
                isinstance(model, dict)
                and model.get("slug") in (required_models - present) & admitted
            ):
                catalog["models"].append(model)
                catalog.setdefault("agentroute_account_models", []).append(model["slug"])
                present.add(model["slug"])
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    by_slug = {
        model["slug"]: model
        for model in catalog["models"]
        if isinstance(model, dict) and isinstance(model.get("slug"), str)
    }
    for alias, canonical in sorted(aliases.items()):
        if canonical not in by_slug:
            raise ValueError(f"installed Codex catalog has no metadata for {alias} ({canonical})")
        model = copy.deepcopy(by_slug[canonical])
        model["slug"] = alias
        model["display_name"] = alias
        catalog["models"] = [item for item in catalog["models"] if item.get("slug") != alias]
        catalog["models"].append(model)
    for descriptor in claude_models:
        # Fill required harness fields from its installed baseline while replacing
        # provider-specific limits, modalities, guidance and tool capabilities.
        model = copy.deepcopy(next(iter(by_slug.values()), {}))
        model.update(descriptor)
        model["auto_compact_token_limit"] = None
        catalog["models"] = [
            item for item in catalog["models"] if item.get("slug") != model["slug"]
        ]
        catalog["models"].append(model)
    known_slugs = {
        model["slug"]
        for model in catalog["models"]
        if isinstance(model, dict) and isinstance(model.get("slug"), str)
    }
    by_slug = {
        model["slug"]: model
        for model in catalog["models"]
        if isinstance(model, dict) and isinstance(model.get("slug"), str)
    }
    for backend_name, backend in routed_backends:
        for target in backend.tiers.values():
            slug = target.model
            capabilities = model_capabilities(config, backend_name, slug)
            if slug in known_slugs or slug in aliases:
                # Account-specific Codex limits apply to the direct GPT backend.
                # A routed provider alias must use its configured window; copying
                # the canonical GPT window would make Azure inherit a smaller
                # ChatGPT entitlement and compact too early.
                if (
                    backend_name != "gpt"
                    and slug not in account_slugs
                    and capabilities.context_window is not None
                    and slug in by_slug
                ):
                    by_slug[slug]["context_window"] = capabilities.context_window
                    # Account-specific compaction caps must not constrain another provider.
                    by_slug[slug]["auto_compact_token_limit"] = None
                continue
            if capabilities.context_window is None:
                continue
            # A compact descriptor intentionally inherits the Codex fallback
            # behavior for provider protocol details while replacing the limits
            # and tool exposure that affect prompt size and compaction.
            model = copy.deepcopy(next(iter(by_slug.values()), {}))
            model.update({
                "slug": slug,
                "display_name": slug,
                "description": f"{backend.display_name} model routed through AgentRoute.",
                "context_window": capabilities.context_window,
                "max_context_window": capabilities.context_window,
                "auto_compact_token_limit": None,
                "effective_context_window_percent": 95,
                "input_modalities": ["text"] + (["image"] if capabilities.vision else []),
                "supports_image_detail_original": bool(capabilities.vision),
                # Plain Responses function support does not imply Codex's special
                # code-mode or tool-search protocols. Do not invent that support.
                "tool_mode": "direct",
                "supports_search_tool": False,
                "multi_agent_version": None,
                "multi_agent_reasoning_effort": None,
            })
            if capabilities.reasoning is False:
                model["default_reasoning_level"] = None
                model["supported_reasoning_levels"] = []
            if not capabilities.vision:
                model["input_modalities"] = ["text"]
                model["supports_image_detail_original"] = False
            catalog["models"].append(model)
            known_slugs.add(slug)
            by_slug[slug] = model
    # An alternate config (including a test fixture) must never overwrite the
    # catalog used by another config or by running Desktop/CLI sessions.
    path = _launch_model_catalog_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(catalog, ensure_ascii=False, separators=(",", ":")) + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != rendered:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as temporary:
            temporary.write(rendered)
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    return path


def refresh_launch_catalog(config: AppConfig) -> Path | None:
    """Repair managed metadata before Codex freezes its catalog at startup.

    Keep a deliberately selected third-party catalog under its owner's control.
    This only writes the generated sidecar; it never rewrites Codex configuration.
    """
    config_path = codex_home() / "config.toml"
    try:
        configured = tomllib.loads(config_path.read_text()).get("model_catalog_json")
    except FileNotFoundError:
        configured = None
    if configured is not None and not _is_managed_model_catalog_path(configured, config_path):
        return None
    return _write_model_catalog(config, config_path=config_path)


def _provider_block(
    config: AppConfig,
    *,
    preserve_reasoning_compatibility: bool,
    model_catalog_path: Path | None = None,
) -> str:
    lines = [START_MARKER]
    if model_catalog_path is not None:
        lines.extend([f"model_catalog_json = {_toml_string(str(model_catalog_path))}", ""])
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
        # Approval review is an isolated, bounded classification task. Default to
        # the backend's cheapest FAST deployment rather than coupling review cost
        # to the worker tier. Operators can still pin a separately validated model.
        review_target = effective_review_model(config, name)
        lines.append(f"approval_review_model = {_toml_string(review_target)}")
        windows = _provider_context_windows(config, name)
        if windows:
            entries = ", ".join(f"{_toml_string(m)} = {w}" for m, w in sorted(windows.items()))
            lines.append(f"capabilities = {{ context_windows = {{ {entries} }} }}")
        tool_compatibility = backend.tool_compatibility
        if (
            tool_compatibility == "functions_and_apply_patch_preserve_reasoning"
            and not preserve_reasoning_compatibility
        ):
            tool_compatibility = "functions_and_apply_patch"
        if tool_compatibility in {
            "functions_and_apply_patch",
            "functions_and_apply_patch_preserve_reasoning",
        }:
            lines.append(f"tool_compatibility = {_toml_string(tool_compatibility)}")
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
    preserve_reasoning_compatibility: bool | None = None,
    codex_source: Path | None = None,
) -> tuple[Path, Path | None]:
    """Replace only AgentRoute's marked provider block in Codex configuration."""
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    path = path or codex_home.expanduser() / "config.toml"
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
    if preserve_reasoning_compatibility is None:
        preserve_reasoning_compatibility = (
            runtime_supports_signed_reasoning_compatibility()
            if any(
                backend.enabled
                and backend.tool_compatibility == "functions_and_apply_patch_preserve_reasoning"
                for backend in config.backends.values()
            )
            else True
        )
    model_catalog_path = _write_model_catalog(config, codex_source, config_path=path)
    block = _provider_block(
        config,
        preserve_reasoning_compatibility=preserve_reasoning_compatibility,
        model_catalog_path=model_catalog_path,
    )
    # A TOML table never closes. Keep root keys before the first table.
    first_table = re.search(r"(?m)^\s*\[[^\n]+\]", current)
    if first_table:
        prefix = current[: first_table.start()].rstrip()
        suffix = current[first_table.start() :].lstrip("\n")
        rendered = (prefix + "\n\n" if prefix else "") + block + "\n" + suffix
    else:
        rendered = current.rstrip() + "\n\n" + block
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
