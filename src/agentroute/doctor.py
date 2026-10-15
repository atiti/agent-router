from __future__ import annotations

import json
import os
import platform
import plistlib
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

from .audit import AuditStore
from .bridge_service import (
    DEFAULT_BRIDGE_PORT,
    probe_health,
    service_installed,
    service_port,
)
from .classifier import JevShadowClassifier, catalog_age_seconds, is_loopback_endpoint, read_api_key
from .config import AppConfig, agentroute_home, codex_home, config_path, model_capabilities
from .install import hook_command
from .profiles import account_credential_home
from .providers import (
    END_MARKER,
    START_MARKER,
    _installed_codex_catalog,
    _is_managed_model_catalog_path,
    backend_readiness,
    refresh_launch_catalog,
)

EXPECTED_RUNTIME_REVISION = "provider-routing-v58"


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    detail: str


def _hooks_check() -> DoctorCheck:
    path = codex_home() / "hooks.json"
    if not path.exists():
        return DoctorCheck("hooks", "fail", f"missing {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return DoctorCheck("hooks", "fail", f"unreadable hooks config: {type(error).__name__}")
    hooks = payload.get("hooks", {})
    expected = {
        "UserPromptSubmit": hook_command("user-prompt-submit"),
        "Stop": hook_command("stop"),
        "SubagentStop": hook_command("stop"),
        "Interrupt": hook_command("interrupt"),
    }
    missing = []
    for event, command in expected.items():
        groups = hooks.get(event, []) if isinstance(hooks, dict) else []
        installed = {
            str(item.get("command", ""))
            for group in groups
            if isinstance(group, dict)
            for item in group.get("hooks", [])
            if isinstance(item, dict)
        }
        if command not in installed:
            missing.append(event)
    if missing:
        return DoctorCheck("hooks", "fail", "missing events: " + ", ".join(missing))
    return DoctorCheck(
        "hooks",
        "pass",
        "UserPromptSubmit, Stop, SubagentStop, and Interrupt are installed",
    )


def _desktop_check(destination: Path, build_id: str) -> DoctorCheck:
    embedded = destination / "Contents" / "Resources" / "codex-bin"
    app_build = "unknown"
    info_path = destination / "Contents" / "Info.plist"
    if info_path.exists():
        with info_path.open("rb") as handle:
            app_build = str(plistlib.load(handle).get("AgentRouteDesktopBuild", "unknown"))
    desktop_ready = embedded.exists() and app_build == build_id
    return DoctorCheck(
        "desktop",
        "pass" if desktop_ready else "warn" if embedded.exists() else "fail",
        "routed app matches local runtime"
        if desktop_ready
        else f"embedded build {app_build}; local build {build_id}"
        if embedded.exists()
        else "routed binary missing",
    )


def _bridge_check(config: AppConfig) -> DoctorCheck:
    """Report whether the Claude bridge backend is installed and answering.

    Only runs when a Claude backend is configured, so an AgentRoute install that
    never opted into the bridge does not collect a permanent warning.
    """
    claude_backends = {
        name: backend
        for name, backend in config.backends.items()
        if name == "claude"
        or (backend.display_name or "").lower().startswith("claude")
        or (backend.codex_provider or "").startswith("agentroute-claude")
    }
    if not claude_backends:
        return DoctorCheck("bridge", "pass", "not configured; no Claude backend")
    if not service_installed():
        return DoctorCheck(
            "bridge",
            "fail",
            "not installed; run `agentroute bridge install`",
        )
    configured_port = service_port()
    port = configured_port or DEFAULT_BRIDGE_PORT
    health = probe_health(port)
    if health.status == "healthy":
        return DoctorCheck("bridge", "pass", f"installed and healthy on 127.0.0.1:{port}")
    if health.status == "unhealthy":
        return DoctorCheck("bridge", "fail", f"installed but {health.detail}")
    return DoctorCheck(
        "bridge",
        "fail",
        f"installed but not answering on 127.0.0.1:{port}; run `agentroute bridge install`",
    )


def _effective_model_catalog(config: AppConfig) -> tuple[dict | None, str]:
    """Load the catalog that a new routed Codex launch will use."""
    config_file = codex_home() / "config.toml"
    try:
        settings = tomllib.loads(config_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        settings = {}
    configured_path = settings.get("model_catalog_json")
    if configured_path is not None and not _is_managed_model_catalog_path(
        configured_path, config_file
    ):
        path = Path(configured_path).expanduser()
        if not path.is_absolute():
            path = config_file.parent / path
        return json.loads(path.read_text(encoding="utf-8")), f"configured catalog {path}"

    path = refresh_launch_catalog(config)
    if path is not None:
        return json.loads(path.read_text(encoding="utf-8")), f"launch catalog {path}"
    catalog = _installed_codex_catalog()
    if catalog is not None:
        return catalog, "installed Codex catalog"
    return None, "installed Codex catalog unavailable"


def _context_windows_check(
    config: AppConfig,
    backend_config_ready: dict[str, bool] | None = None,
) -> DoctorCheck:
    """Compare configured model limits with the catalog used by new Codex launches."""
    try:
        catalog, source = _effective_model_catalog(config)
    except (OSError, ValueError, TypeError) as error:
        return DoctorCheck(
            "context-windows",
            "fail",
            f"cannot load effective Codex model catalog: {type(error).__name__}: {error}",
        )
    if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
        return DoctorCheck(
            "context-windows",
            "warn",
            f"{source}; model window sizes cannot be cross-checked",
        )

    by_slug = {
        model["slug"]: model
        for model in catalog["models"]
        if isinstance(model, dict) and isinstance(model.get("slug"), str)
    }
    failures: list[str] = []
    warnings: list[str] = []
    details: list[str] = []
    for backend_name, backend in sorted(config.backends.items()):
        if not backend.enabled:
            continue
        model_names = {target.model for target in backend.tiers.values()}
        if backend.review_model:
            model_names.add(backend.review_model)
        for model_name in sorted(model_names):
            capabilities = model_capabilities(config, backend_name, model_name)
            declared = capabilities.context_window
            slugs = [model_name]
            if model_name.startswith("dev-"):
                slugs.append(model_name.removeprefix("dev-"))
            if model_name in config.pricing.aliases:
                slugs.append(config.pricing.aliases[model_name])
            descriptor = next((by_slug[slug] for slug in slugs if slug in by_slug), None)
            if descriptor is None:
                if declared is not None:
                    detail = (
                        f"{backend_name}/{model_name}: configured {declared:,} tokens, "
                        "missing from effective Codex catalog"
                    )
                else:
                    detail = f"{backend_name}/{model_name}: context window unknown; missing"
                (failures if declared is not None else warnings).append(detail)
                continue

            current = descriptor.get("context_window")
            maximum = descriptor.get("max_context_window")
            valid_current = type(current) is int and current > 0
            valid_maximum = type(maximum) is int and maximum > 0
            if not valid_current:
                warnings.append(f"{backend_name}/{model_name}: catalog context window missing")
            if not valid_maximum:
                warnings.append(f"{backend_name}/{model_name}: catalog maximum window missing")
            if valid_current and valid_maximum and current > maximum:
                failures.append(
                    f"{backend_name}/{model_name}: catalog context {current:,} exceeds "
                    f"maximum {maximum:,}"
                )
            if declared is not None and valid_maximum and declared > maximum:
                failures.append(
                    f"{backend_name}/{model_name}: configured {declared:,} exceeds "
                    f"Codex maximum {maximum:,}"
                )
            if declared is not None and valid_current and current > declared:
                warnings.append(
                    f"{backend_name}/{model_name}: Codex currently allows {current:,}, "
                    f"above configured {declared:,}"
                )

            window_detail = f"{backend_name}/{model_name}: "
            if valid_current:
                window_detail += f"current {current:,}"
            else:
                window_detail += "current unknown"
            if valid_maximum:
                window_detail += f", maximum {maximum:,}"
            if declared is not None:
                window_detail += f", configured {declared:,}"
            if backend_name != "gpt":
                window_detail += "; configured locally, provider limit unverified"
                warnings.append(
                    f"{backend_name}/{model_name}: provider-reported context limit is "
                    "unavailable to verify the configured value"
                )
            if backend_config_ready is not None and not backend_config_ready.get(
                backend_name, True
            ):
                window_detail += "; backend configuration incomplete"
                warnings.append(
                    f"{backend_name}/{model_name}: backend configuration incomplete; "
                    "provider limit unverified"
                )
            details.append(window_detail)

    status = "fail" if failures else "warn" if warnings else "pass"
    summary = failures + warnings + details
    if not summary:
        return DoctorCheck("context-windows", "warn", "no enabled model targets")
    return DoctorCheck("context-windows", status, f"{source}; " + "; ".join(summary))


def run_doctor(config: AppConfig) -> tuple[DoctorCheck, ...]:
    """Run local, non-networked checks without exposing credentials."""
    checks: list[DoctorCheck] = []
    checks.append(
        DoctorCheck(
            "config",
            "pass" if config_path().exists() else "warn",
            f"loaded {config_path()}" if config_path().exists() else "using bundled defaults",
        )
    )
    if not config.capacity.enabled:
        checks.append(DoctorCheck("capacity", "warn", "disabled; no quota or budget failover"))
    else:
        problems: list[str] = []
        if config.capacity.warn_percent > config.capacity.switch_percent:
            problems.append("warn threshold exceeds switch threshold")
        for name, backend in config.backends.items():
            if backend.fallback_backend and backend.fallback_backend not in config.backends:
                problems.append(f"{name} fallback is unknown: {backend.fallback_backend}")
            if backend.fallback_backend == name:
                problems.append(f"{name} fallback points to itself")
        checks.append(
            DoctorCheck(
                "capacity",
                "fail" if problems else "pass",
                "; ".join(dict.fromkeys(problems))
                if problems
                else (
                    f"enabled; warn {config.capacity.warn_percent:g}%; switch "
                    f"{config.capacity.switch_percent:g}%"
                ),
            )
        )
        active = config.capacity.active_profile
        missing = [
            name
            for name, profile in config.capacity.profiles.items()
            if profile.enabled and not account_credential_home(name, profile).is_dir()
        ]
        if active and active not in config.capacity.profiles:
            checks.append(
                DoctorCheck("capacity-accounts", "fail", f"unknown active account: {active}")
            )
        elif missing:
            checks.append(
                DoctorCheck(
                    "capacity-accounts",
                    "warn",
                    "missing credential slots: " + ", ".join(sorted(missing)),
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    "capacity-accounts",
                    "pass",
                    f"{len(config.capacity.profiles)} configured; active {active or 'default'}; "
                    f"canonical home {codex_home()}",
                )
            )
    checks.append(
        DoctorCheck(
            "routing",
            "pass" if config.enabled else "warn",
            "enabled" if config.enabled else "observe-only mode",
        )
    )

    home = agentroute_home()
    binary = home / "bin" / "codex-bin"
    build_path = home / "build-id"
    build_id = build_path.read_text(encoding="utf-8").strip() if build_path.exists() else "missing"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        checks.append(DoctorCheck("runtime", "fail", f"missing executable {binary}"))
    elif EXPECTED_RUNTIME_REVISION not in build_id:
        checks.append(DoctorCheck("runtime", "warn", f"outdated build {build_id}"))
    else:
        checks.append(DoctorCheck("runtime", "pass", build_id))

    checks.append(_hooks_check())
    provider_path = codex_home() / "config.toml"
    provider_text = provider_path.read_text(encoding="utf-8") if provider_path.exists() else ""
    external_enabled = any(
        name != "gpt" and backend.enabled for name, backend in config.backends.items()
    )
    provider_ok = START_MARKER in provider_text and END_MARKER in provider_text
    checks.append(
        DoctorCheck(
            "provider-config",
            "pass" if provider_ok or not external_enabled else "fail",
            "managed provider block present"
            if provider_ok
            else "no managed provider block required"
            if not external_enabled
            else f"missing managed provider block in {provider_path}",
        )
    )

    try:
        store = AuditStore()
        with store.connection() as connection:
            result = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        checks.append(DoctorCheck("audit-db", "pass" if result == "ok" else "fail", result))
    except Exception as error:
        checks.append(DoctorCheck("audit-db", "fail", type(error).__name__))

    backend_config_ready: dict[str, bool] = {}
    for name, backend in config.backends.items():
        ready, problems = backend_readiness(config, name)
        backend_config_ready[name] = ready
        status = "pass" if ready else "warn" if not backend.enabled else "fail"
        checks.append(
            DoctorCheck(
                f"backend-config:{name}",
                status,
                f"{backend.display_name}; provider not contacted"
                if ready
                else ", ".join(problems),
            )
        )
        if backend.enabled:
            unknown = sorted(
                {
                    target.model
                    for target in backend.tiers.values()
                    if model_capabilities(config, name, target.model).tool_calling == "unknown"
                }
            )
            if unknown:
                checks.append(
                    DoctorCheck(
                        f"capabilities:{name}",
                        "warn",
                        "unknown tool compatibility: " + ", ".join(unknown),
                    )
                )
            unpriced = sorted(
                {
                    target.model
                    for target in backend.tiers.values()
                    if (
                        model_capabilities(config, name, target.model).pricing_model
                        or target.model
                    )
                    not in config.pricing.models
                    and (
                        model_capabilities(config, name, target.model).pricing_model
                        or target.model
                    )
                    not in config.pricing.aliases
                }
            )
            if unpriced:
                checks.append(
                    DoctorCheck(
                        f"pricing:{name}",
                        "warn",
                        "unpriced models: " + ", ".join(unpriced),
                    )
                )

    checks.append(_context_windows_check(config, backend_config_ready))

    classifier = config.routing.classifier
    if not classifier.enabled:
        checks.append(DoctorCheck("classifier", "warn", "disabled; deterministic routing only"))
    elif classifier.engine == "jev":
        active = classifier.jev_shadow
        try:
            JevShadowClassifier(active)
            remote = not is_loopback_endpoint(active.endpoint)
            if remote and not active.allow_remote:
                checks.append(DoctorCheck("classifier", "fail", "remote JEV prompt egress blocked"))
            elif remote and not read_api_key(active):
                checks.append(DoctorCheck("classifier", "fail", "JEV credential missing"))
            elif remote and (
                (age := catalog_age_seconds(active)) is None or age > active.catalog_ttl_seconds
            ):
                checks.append(DoctorCheck("classifier", "warn", "JEV catalog stale or unverified"))
            else:
                checks.append(
                    DoctorCheck("classifier", "pass", f"{active.model}; {active.endpoint}")
                )
        except (OSError, RuntimeError, ValueError) as error:
            checks.append(DoctorCheck("classifier", "fail", str(error)))
    else:
        try:
            credential_ready = bool(read_api_key(classifier)) or classifier.endpoint.startswith(
                ("http://127.0.0.1", "http://localhost", "http://[::1]")
            )
            age = catalog_age_seconds(classifier)
            catalog_ready = age is not None and age <= classifier.catalog_ttl_seconds
            if not credential_ready:
                checks.append(DoctorCheck("classifier", "fail", "credential missing"))
            elif not catalog_ready:
                checks.append(
                    DoctorCheck(
                        "classifier", "warn", "model catalog is stale or unverified"
                    )
                )
            else:
                checks.append(
                    DoctorCheck(
                        "classifier", "pass", f"{classifier.model}; catalog age {age:.0f}s"
                    )
                )
        except Exception as error:
            checks.append(DoctorCheck("classifier", "fail", type(error).__name__))

    checks.append(_bridge_check(config))

    if platform.system() == "Darwin":
        destination = Path("/Applications/ChatGPT-Routed.app")
        if destination.exists():
            checks.append(_desktop_check(destination, build_id))
    return tuple(checks)
