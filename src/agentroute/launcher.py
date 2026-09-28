from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import AppConfig, agentroute_home, load_config
from .context_profile import capture_enabled, profile_root

BASE_CODEX_ARGS = [
    "--enable",
    "step_model_switching",
    "--enable",
    "code_mode",
    "--enable",
    "code_mode_host",
    "-c",
    "suppress_unstable_features_warning=true",
]


def _has_config_override(args: Sequence[str], key: str) -> bool:
    assignment = f"{key}="
    return any(assignment in arg for arg in args)


def _has_model_override(args: Sequence[str]) -> bool:
    return any(
        arg in {"-m", "--model"} or arg.startswith("--model=")
        for arg in args
    ) or _has_config_override(args, "model")


def startup_route_args(config: AppConfig, user_args: Sequence[str]) -> list[str]:
    """Select a safe initial provider before any prompt or compaction request runs."""
    if _has_config_override(user_args, "model_provider"):
        return []
    backend_name = config.routing.backend_by_tier.get("normal", "gpt")
    backend = config.backends.get(backend_name)
    if backend is None or not backend.enabled:
        backend = config.backends["gpt"]
    target = backend.tiers["normal"]
    args = ["-c", f'model_provider="{backend.codex_provider}"']
    if not _has_model_override(user_args):
        args.extend(["--model", target.model])
    if target.reasoning_effort and not _has_config_override(
        user_args, "model_reasoning_effort"
    ):
        args.extend(["-c", f'model_reasoning_effort="{target.reasoning_effort}"'])
    return args


def codex_argv(
    binary: Path, user_args: Sequence[str], config: AppConfig | None = None
) -> list[str]:
    config = config or load_config()
    return [
        str(binary),
        *BASE_CODEX_ARGS,
        *startup_route_args(config, user_args),
        *user_args,
    ]


def launch_codex(
    binary: Path | None, user_args: Sequence[str], profile: str | None = None
) -> None:
    binary = binary or agentroute_home() / "bin" / "codex-bin"
    config = load_config()
    if profile is not None:
        print(
            "◆ ACCOUNT NOTICE · --profile no longer changes CODEX_HOME; "
            "select accounts at the next routed turn boundary",
            file=sys.stderr,
        )
    environment = os.environ.copy()
    if capture_enabled() and not environment.get("CODEX_ROLLOUT_TRACE_ROOT"):
        profile_root().mkdir(parents=True, exist_ok=True, mode=0o700)
        profile_root().chmod(0o700)
        root = profile_root() / "traces"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment["CODEX_ROLLOUT_TRACE_ROOT"] = str(root)
    argv = codex_argv(binary, user_args, config)
    os.execve(str(binary), argv, environment)
