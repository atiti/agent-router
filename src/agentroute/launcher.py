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


def desktop_server_launch(binary: Path, user_args: Sequence[str]) -> bool:
    """The routed Desktop starts app-server over stdio; join our owner via a transport adapter."""
    if not (binary.parent / "agentroute-build-id").exists():
        return False
    if _launch_command(user_args) != "app-server":
        return False
    index = list(user_args).index("app-server")
    server_args = list(user_args)[index + 1 :]
    for index, arg in enumerate(server_args):
        if arg == "--listen" and (
            index + 1 == len(server_args) or server_args[index + 1] != "stdio://"
        ):
            return False
        if arg.startswith("--listen=") and arg != "--listen=stdio://":
            return False
    return not any(arg in {"--help", "-h"} for arg in server_args) and not any(
        arg in {"daemon", "proxy", "generate-ts", "generate-json-schema", "help"}
        for arg in server_args
    )


def _config_assignments(args: Sequence[str]) -> dict[str, str]:
    assignments: dict[str, str] = {}
    iterator = iter(args)
    for arg in iterator:
        if arg in {"-c", "--config"}:
            assignment = next(iterator, "")
        elif arg.startswith("--config="):
            assignment = arg[len("--config=") :]
        elif arg in {"--enable", "--disable"}:
            assignments[f"features.{next(iterator, '')}"] = "true" if arg == "--enable" else "false"
            continue
        else:
            continue
        if "=" in assignment:
            key, value = assignment.split("=", 1)
            assignments[key.strip()] = value.strip()
    return assignments


def validate_desktop_startup(user_args: Sequence[str], config: AppConfig) -> None:
    from .shared_server import SharedServerError, server_argv

    configured = _config_assignments(server_argv(agentroute_home() / "bin" / "codex-bin", config))
    requested = _config_assignments(user_args)
    missing = [key for key, value in requested.items() if configured.get(key) != value]
    if (
        "--analytics-default-enabled" in user_args
        and not config.shared_server.analytics_default_enabled
    ):
        missing.append("analytics default")
    if missing:
        raise SharedServerError(
            "Desktop startup settings are missing from the shared owner: "
            + ", ".join(missing)
            + ". Set them with `agentroute server configure`, "
            "then finish its sessions and stop/start."
        )


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
    desktop_proxy = config.shared_server.enabled and desktop_server_launch(binary, user_args)
    if desktop_proxy:
        from .shared_server import SharedServerError, ensure_server

        try:
            validate_desktop_startup(user_args, config)
            # CLI and Desktop carry separate copies of the same build. Always own sessions
            # with the canonical binary so their launch fingerprints agree.
            endpoint = ensure_server(agentroute_home() / "bin" / "codex-bin", config, environment)
        except (OSError, SharedServerError) as error:
            print(f"AgentRoute shared server: {error}", file=sys.stderr)
            raise SystemExit(1) from error
        argv = [sys.executable, "-m", "agentroute.shared_server", "proxy", str(endpoint)]
    elif config.shared_server.enabled and interactive_launch(user_args) and not explicit_remote:
        from .shared_server import SharedServerError, ensure_server, socket_path

        try:
            client_args = shared_client_args(user_args, socket_path())
            ensure_server(binary, config, environment)
        except (OSError, SharedServerError) as error:
            print(f"AgentRoute shared server: {error}", file=sys.stderr)
            raise SystemExit(1) from error
        argv[1:1] = client_args
    os.execve(argv[0], argv, environment)
