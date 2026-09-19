from __future__ import annotations

import json
import os
import platform
import plistlib
from dataclasses import dataclass
from pathlib import Path

from .audit import AuditStore
from .classifier import catalog_age_seconds, read_api_key
from .config import AppConfig, agentroute_home, config_path, model_capabilities
from .install import hook_command
from .providers import END_MARKER, START_MARKER, backend_readiness

EXPECTED_RUNTIME_REVISION = "provider-routing-v28"


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    detail: str


def _hooks_check() -> DoctorCheck:
    path = Path.home() / ".codex" / "hooks.json"
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
        "hooks", "pass", "UserPromptSubmit, Stop, and SubagentStop are installed"
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


def run_doctor(config: AppConfig) -> tuple[DoctorCheck, ...]:
    """Run local, non-networked health checks without exposing credentials."""
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
        if not config.capacity.profiles:
            checks.append(
                DoctorCheck(
                    "capacity-profiles",
                    "warn",
                    "no isolated ChatGPT profiles; API backend failover remains available",
                )
            )
        else:
            active = config.capacity.active_profile
            missing = [
                name
                for name, profile in config.capacity.profiles.items()
                if profile.enabled and not Path(profile.codex_home).expanduser().is_dir()
            ]
            if active and active not in config.capacity.profiles:
                checks.append(
                    DoctorCheck("capacity-profiles", "fail", f"unknown active profile: {active}")
                )
            elif missing:
                checks.append(
                    DoctorCheck(
                        "capacity-profiles",
                        "warn",
                        "missing CODEX_HOME directories: " + ", ".join(sorted(missing)),
                    )
                )
            else:
                checks.append(
                    DoctorCheck(
                        "capacity-profiles",
                        "pass",
                        f"{len(config.capacity.profiles)} configured; active {active or 'auto'}",
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
    provider_path = Path.home() / ".codex" / "config.toml"
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

    for name, backend in config.backends.items():
        ready, problems = backend_readiness(config, name)
        status = "pass" if ready else "warn" if not backend.enabled else "fail"
        checks.append(
            DoctorCheck(
                f"backend:{name}",
                status,
                backend.display_name if ready else ", ".join(problems),
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

    classifier = config.routing.classifier
    if not classifier.enabled:
        checks.append(DoctorCheck("classifier", "warn", "disabled; deterministic routing only"))
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

    if platform.system() == "Darwin":
        destination = Path("/Applications/ChatGPT-Routed.app")
        if destination.exists():
            checks.append(_desktop_check(destination, build_id))
    return tuple(checks)
