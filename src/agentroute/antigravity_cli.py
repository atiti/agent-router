"""Antigravity subscription backend: lifecycle and Codex registration."""

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
    TIER_EFFORT,
    binary,
    workspace,
)
from .antigravity_usage import fetch_limits
from .bridge_service import _agentroute_executable, wait_for_health
from .config import (
    ExecutionBackendConfig,
    ModelCapabilities,
    ModelTarget,
    load_config,
    save_config,
)
from .gemini_bridge import price_family
from .providers import sync_codex_providers

DEFAULT_PORT = 8816
LABEL = "com.agentroute.antigravity-bridge"
UNIT = "agentroute-antigravity-bridge.service"

app = typer.Typer(
    no_args_is_help=True,
    help="Route Codex through the Antigravity subscription using the agy CLI.",
)


def unit_path() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    return Path.home() / ".config/systemd/user" / UNIT


def environment(resolved: str | None = None) -> dict[str, str]:
    """Variables the service needs, with the CLI pinned by absolute path.

    launchd and systemd start with a minimal PATH that excludes ~/.local/bin, so
    a service that resolves `agy` from PATH answers health checks and then fails
    every turn. Pinning the path chosen at install time avoids that.
    """
    keys = (
        "AGENTROUTE_ANTIGRAVITY_CWD",
        "AGENTROUTE_ANTIGRAVITY_MODE",
    )
    values = {key: os.environ[key] for key in keys if os.environ.get(key)}
    if resolved:
        values["AGENTROUTE_ANTIGRAVITY_BINARY"] = resolved
    return values


def ensure_backend(config, port: int = DEFAULT_PORT) -> bool:
    """Register the Antigravity backend without changing default tier routes."""
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
            display_name="Antigravity subscription (agy)",
            base_url=f"http://127.0.0.1:{port}/v1",
            tool_compatibility="functions_and_apply_patch",
            review_model=MODELS["fast"],
            tiers=tiers,
        )
    else:
        backend = config.backends["antigravity"]
        backend.enabled = True
        backend.base_url = f"http://127.0.0.1:{port}/v1"
        backend.tiers = tiers
    for model in MODELS.values():
        declared = config.capabilities.models.get(model)
        update = {
            # agy runs its own tools inside the turn; Codex must not also.
            "tool_calling": "none",
            "reasoning": True,
            "vision": False,
            "context_window": CONTEXT_WINDOW,
            "pricing_model": price_family(model),
        }
        config.capabilities.models[model] = (
            declared.model_copy(update=update) if declared else ModelCapabilities(**update)
        )
        # Informational API-equivalent rates: the subscription is flat, but token
        # volume is still measured and must not disappear from reports.
        config.pricing.families[model] = price_family(model)
    return created


def service_definition(port: int, resolved: str | None = None) -> dict:
    log_path = Path.home() / ".agentroute" / "antigravity-bridge.log"
    definition = {
        "Label": LABEL,
        "ProgramArguments": [
            str(_agentroute_executable()),
            "antigravity",
            "serve",
            "--port",
            str(port),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    values = environment(resolved)
    if values:
        definition["EnvironmentVariables"] = values
    return definition


def systemd_unit_text(port: int, resolved: str | None = None) -> str:
    lines = [
        "[Unit]",
        "Description=AgentRoute Antigravity bridge (agy subscription to Responses)",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
    ]
    lines.extend(f"Environment={key}={value}" for key, value in environment(resolved).items())
    lines.extend(
        [
            f"ExecStart={_agentroute_executable()} antigravity serve --port {port}",
            "Restart=always",
            "RestartSec=3",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    return "\n".join(lines)


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


@app.command("check")
def check_command() -> None:
    """Verify the CLI, sign-in and subscription allowance, then show the tiers."""
    resolved = shutil.which(binary())
    if not resolved:
        typer.echo(f"Antigravity CLI not found ({binary()}). Install it and run `agy` once.")
        raise typer.Exit(1)
    typer.echo(f"agy:       {resolved}")
    typer.echo(f"workspace: {workspace()}")
    for tier, model in MODELS.items():
        typer.echo(f"{tier:6} {model}  ({TIER_EFFORT[tier]} effort)")
    limits = fetch_limits(binary=resolved)
    if not limits:
        typer.echo("subscription: unavailable (run `agy` once to sign in)")
        raise typer.Exit(1)
    typer.echo(f"subscription ({limits['observed_at']}):")
    for group in limits["groups"]:
        reset = f" · resets {group['resets_at']}" if group.get("resets_at") else ""
        typer.echo(f"  {str(group['name']):26} {group['remaining_percent']}% remaining{reset}")


@app.command("usage")
def usage_command() -> None:
    """Show the Antigravity subscription allowance."""
    limits = fetch_limits(binary=shutil.which(binary()))
    if not limits:
        typer.echo("subscription allowance unavailable; run `agy` once to sign in")
        raise typer.Exit(1)
    typer.echo(f"Antigravity subscription ({limits['observed_at']}):")
    for group in limits["groups"]:
        reset = f" · resets {group['resets_at']}" if group.get("resets_at") else ""
        typer.echo(f"  {str(group['name']):26} {group['remaining_percent']}% remaining{reset}")


@app.command("serve")
def serve_command(
    port: int = typer.Option(DEFAULT_PORT, help="Loopback port to bind."),
    host: str = typer.Option("127.0.0.1", help="Loopback host to bind."),
) -> None:
    """Run the Antigravity bridge in the foreground."""
    from .antigravity_bridge import serve

    serve(host=host, port=port)


@app.command("install")
def install_command(
    port: int = typer.Option(DEFAULT_PORT, help="Loopback port to bind."),
    start: bool = typer.Option(
        True, "--service/--no-service", help="Start the background service."
    ),
) -> None:
    """Register the backend and start the bridge."""
    resolved = shutil.which(binary())
    if not resolved:
        typer.echo(f"Antigravity CLI not found ({binary()}); install it and run `agy` once.")
        raise typer.Exit(1)
    config = load_config()
    ensure_backend(config, port)
    save_config(config)
    sync_codex_providers(config)
    typer.echo(f"registered backend `antigravity` at http://127.0.0.1:{port}/v1")
    for tier, model in MODELS.items():
        typer.echo(f"  {tier:6} {model}")
    if not start:
        return
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        path.write_bytes(plistlib.dumps(service_definition(port, resolved)))
    else:
        path.write_text(systemd_unit_text(port, resolved))
    load_service(path)
    health = wait_for_health(port, host="127.0.0.1", timeout=25)
    if health.status == "unreachable":
        typer.echo(f"installed at {path}, but the bridge did not answer: {health.detail}")
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
