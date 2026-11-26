"""Antigravity (Gemini) bridge lifecycle."""

from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
from pathlib import Path

import typer

from .antigravity_bridge import (
    CONTEXT_WINDOW,
    MODELS,
    AntigravityCredential,
    CredentialError,
    resolve_agy,
    serve,
)
from .bridge_service import _agentroute_executable, wait_for_health
from .config import (
    ExecutionBackendConfig,
    ModelCapabilities,
    ModelTarget,
    load_config,
    save_config,
)
from .providers import sync_codex_providers

DEFAULT_PORT = 8816
LABEL = "com.agentroute.antigravity-bridge"
UNIT = "agentroute-antigravity-bridge.service"
TIER_EFFORT = {"fast": "low", "normal": "medium", "smart": "high", "max": "xhigh"}

app = typer.Typer(no_args_is_help=True, help="Route Codex through your Antigravity (Gemini) login.")


def unit_path() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    return Path.home() / ".config/systemd/user" / UNIT


def ensure_backend(config, port: int = DEFAULT_PORT) -> bool:
    """Register the Antigravity backend without changing default tier routing."""
    created = "antigravity" not in config.backends
    tiers = {
        tier: ModelTarget(
            model=model,
            reasoning_effort=TIER_EFFORT[tier],
            context_window=CONTEXT_WINDOW,
        )
        for tier, model in MODELS.items()
    }
    if created:
        config.backends["antigravity"] = ExecutionBackendConfig(
            enabled=True,
            codex_provider="agentroute-antigravity",
            display_name="Antigravity Gemini (OAuth subscription)",
            base_url=f"http://127.0.0.1:{port}/v1",
            tool_compatibility="functions_and_apply_patch_preserve_reasoning",
            review_model=MODELS["fast"],
            tiers=tiers,
        )
    else:
        config.backends["antigravity"].enabled = True
        config.backends["antigravity"].base_url = f"http://127.0.0.1:{port}/v1"
    for model in MODELS.values():
        config.capabilities.models.setdefault(
            model,
            ModelCapabilities(
                # The bridge delegates to `agy`, which runs its own tool loop and
                # returns finished text. Codex therefore receives no tool calls
                # and cannot execute any of its own tools on this backend.
                tool_calling="none",
                reasoning=True,
                vision=True,
                context_window=CONTEXT_WINDOW,
            ),
        )
    return created


def service_definition(port: int, binary: str) -> dict:
    log_path = Path.home() / ".agentroute" / "antigravity-bridge.log"
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(_agentroute_executable()),
            "antigravity",
            "serve",
            "--port",
            str(port),
            "--agy-binary",
            binary,
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


def systemd_unit_text(port: int, binary: str) -> str:
    return (
        "[Unit]\n"
        "Description=AgentRoute Antigravity bridge (Gemini OAuth to Responses)\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={_agentroute_executable()} antigravity serve --port {port} "
        f"--agy-binary {binary}\n"
        "Restart=always\n"
        "RestartSec=3\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _run(*command: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), capture_output=True, text=True, check=check)


def load_service(path: Path) -> None:
    if platform.system() == "Darwin":
        _run("launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}")
        _run("launchctl", "bootstrap", f"gui/{os.getuid()}", str(path), check=True)
        return
    _run("systemctl", "--user", "daemon-reload", check=True)
    _run("systemctl", "--user", "enable", "--now", UNIT, check=True)


def unload_service() -> None:
    if platform.system() == "Darwin":
        _run("launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}")
        return
    _run("systemctl", "--user", "disable", "--now", UNIT)


@app.command("serve")
def serve_command(
    port: int = typer.Option(DEFAULT_PORT, help="Loopback port to bind."),
    host: str = typer.Option("127.0.0.1", help="Loopback host to bind."),
    agy_binary: str = typer.Option("agy", help="Antigravity CLI executable."),
) -> None:
    """Run the Antigravity bridge in the foreground."""
    serve(host=host, port=port, executable=agy_binary)


@app.command("check")
def check_command() -> None:
    """Verify the Antigravity credential and print the resolved model tiers."""
    try:
        credential = AntigravityCredential()
        credential.token()
    except CredentialError as exc:
        typer.echo(f"Antigravity credential unavailable: {exc}")
        raise typer.Exit(1) from exc
    typer.echo(f"credential: {credential.source()}")
    for tier, model in MODELS.items():
        typer.echo(f"{tier:6} {model}")
    typer.echo(f"agy: {shutil.which('agy') or 'not found'}")


@app.command("install")
def install_command(
    port: int = typer.Option(DEFAULT_PORT, help="Loopback port to bind."),
    start: bool = typer.Option(
        True, "--service/--no-service", help="Start the background service."
    ),
) -> None:
    """Register the backend and start the bridge service."""
    try:
        binary = resolve_agy()
    except CredentialError as exc:
        typer.echo(f"{exc}. Sign in with `agy` first.")
        raise typer.Exit(1) from exc
    try:
        AntigravityCredential().token()
    except CredentialError as exc:
        typer.echo(f"Antigravity credential unavailable: {exc}")
        raise typer.Exit(1) from exc
    config = load_config()
    ensure_backend(config, port)
    save_config(config)
    sync_codex_providers(config)
    typer.echo(f"registered backend `antigravity` at http://127.0.0.1:{port}/v1")
    if not start:
        return
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        path.write_bytes(plistlib.dumps(service_definition(port, binary)))
    else:
        path.write_text(systemd_unit_text(port, binary))
    load_service(path)
    health = wait_for_health(port, host="127.0.0.1", timeout=20)
    if health.status == "unreachable":
        typer.echo(
        f"installed at {path}, but the bridge did not answer: {health.detail}"
    )
        raise typer.Exit(1)
    typer.echo(f"bridge healthy at http://127.0.0.1:{port} ({health.detail})")


@app.command("uninstall")
def uninstall_command() -> None:
    """Stop the bridge service and remove its unit."""
    unload_service()
    path = unit_path()
    if path.exists():
        path.unlink()
    typer.echo(f"stopped Antigravity bridge ({path})")

