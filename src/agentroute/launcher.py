from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import AppConfig, agentroute_home, load_config
from .profiles import select_launch_profile

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
    selected_name, selected, _ = select_launch_profile(
        config, profile, codex_binary=binary
    )
    environment = os.environ.copy()
    if selected_name:
        selected_config = config.capacity.profiles[selected_name]
        environment["CODEX_HOME"] = str(Path(selected_config.codex_home).expanduser())
        state = selected.capacity.status if selected else "unknown"
        detail = selected.capacity.detail if selected else "not probed"
        print(
            f"◆ CAPACITY PROFILE · {selected_name} · {state}: {detail}",
            file=sys.stderr,
        )
        if profile is None and selected_name != config.capacity.active_profile:
            print(
                "◆ PROFILE FAILOVER · starting a new Codex process with this profile; "
                "active sessions can also switch profiles at a turn boundary",
                file=sys.stderr,
            )
    argv = codex_argv(binary, user_args, config)
    os.execve(str(binary), argv, environment)
