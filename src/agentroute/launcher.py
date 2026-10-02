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
        arg in {"-m", "--model"} or arg.startswith("--model=") for arg in args
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
    if target.reasoning_effort and not _has_config_override(user_args, "model_reasoning_effort"):
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


def _launch_command(user_args: Sequence[str]) -> str:
    value_options = {
        "-c",
        "--config",
        "--enable",
        "--disable",
        "-m",
        "--model",
        "-C",
        "--cd",
        "-s",
        "--sandbox",
        "-a",
        "--ask-for-approval",
        "-i",
        "--image",
        "-p",
        "--profile",
        "--remote",
        "--remote-auth-token-env",
        "--add-dir",
        "--local-provider",
    }
    commands = {
        "exec",
        "e",
        "review",
        "login",
        "logout",
        "mcp",
        "mcp-server",
        "app-server",
        "app",
        "completion",
        "sandbox",
        "debug",
        "apply",
        "a",
        "resume",
        "fork",
        "cloud",
        "features",
        "remote-control",
        "doctor",
        "update",
        "help",
        "exec-server",
    }
    skip_value = False
    for arg in user_args:
        if skip_value:
            skip_value = False
            continue
        if arg == "--":
            return "start"  # Everything after -- is the initial prompt.
        if arg in value_options:
            skip_value = True
        elif not arg.startswith("-"):
            return arg if arg in commands else "start"
    return "start"


def interactive_launch(user_args: Sequence[str]) -> bool:
    """Only TUI start/resume/fork commands attach; utility commands stay native."""
    if any(arg in {"--help", "-h", "--version", "-V"} for arg in user_args):
        return False
    return _launch_command(user_args) in {"start", "resume", "fork"}


def shared_client_args(user_args: Sequence[str], endpoint: Path) -> list[str]:
    if _has_config_override(user_args, "model_provider") or any(
        arg in {"--oss", "-p", "--profile"} or arg.startswith("--profile=") for arg in user_args
    ):
        from .shared_server import SharedServerError

        raise SharedServerError(
            "shared sessions use the server's provider/profile; choose a routed @backend tag "
            "or disable shared mode with `agentroute server disable` for this configuration"
        )
    args = ["--remote", f"unix://{endpoint}"]
    if _launch_command(user_args) == "start" and not any(
        arg in {"-C", "--cd"} or arg.startswith("--cd=") or arg.startswith("-C")
        for arg in user_args
    ):
        args.extend(["--cd", str(Path.cwd())])
    return args


def launch_codex(binary: Path | None, user_args: Sequence[str], profile: str | None = None) -> None:
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
    explicit_remote = any(arg == "--remote" or arg.startswith("--remote=") for arg in user_args)
    if config.shared_server.enabled and interactive_launch(user_args) and not explicit_remote:
        from .shared_server import SharedServerError, ensure_server, socket_path

        try:
            client_args = shared_client_args(user_args, socket_path())
            ensure_server(binary, config, environment)
        except (OSError, SharedServerError) as error:
            print(f"AgentRoute shared server: {error}", file=sys.stderr)
            raise SystemExit(1) from error
        argv[1:1] = client_args
    os.execve(str(binary), argv, environment)
