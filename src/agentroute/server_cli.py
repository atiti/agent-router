"""CLI controls for the shared routed session owner."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from .config import agentroute_home, load_config, save_config
from .shared_server import (
    SharedServerError,
    ensure_server,
    pair_server,
    server_status,
    stop_server,
)

server_app = typer.Typer(no_args_is_help=True, help="Share routed sessions between CLI and mobile.")


def _start(binary: Path | None = None) -> Path:
    return ensure_server(binary or agentroute_home() / "bin" / "codex-bin", load_config())


@server_app.command("enable")
def enable_command() -> None:
    """Start the shared owner and attach future interactive CLI launches."""
    try:
        endpoint = _start()
    except (OSError, SharedServerError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from error
    config = load_config()
    config.shared_server.enabled = True
    save_config(config)
    typer.echo(f"Shared sessions enabled: unix://{endpoint}")
    typer.echo("Run `agentroute server pair` to connect mobile to this owner.")


@server_app.command("disable")
def disable_command() -> None:
    """Use embedded servers for future CLI launches; existing shared sessions keep running."""
    config = load_config()
    config.shared_server.enabled = False
    save_config(config)
    typer.echo("Shared attachment disabled for future CLI launches.")


@server_app.command("start")
def start_command(binary: Path | None = typer.Option(None)) -> None:
    """Start or reuse the patched owner without changing launcher configuration."""
    try:
        typer.echo(f"unix://{_start(binary)}")
    except (OSError, SharedServerError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from error


@server_app.command("status")
def status_command() -> None:
    """Show local ownership and mobile relay status without starting a server."""
    typer.echo(json.dumps(server_status(), indent=2))


@server_app.command("pair")
def pair_command() -> None:
    """Create a short-lived mobile pairing code for this shared owner."""
    try:
        _start()
        pairing = pair_server()
    except (OSError, SharedServerError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from error
    code = pairing.get("manualPairingCode") or pairing["pairingCode"]
    typer.echo(f"Mobile pairing code: {code}")
    typer.echo(f"Expires at Unix time: {pairing['expiresAt']}")


@server_app.command("stop")
def stop_command(
    force: bool = typer.Option(False, help="Allow interrupting loaded sessions."),
) -> None:
    """Stop the owner, refusing to interrupt loaded sessions unless --force is explicit."""
    try:
        stopped = stop_server(force)
    except (OSError, SharedServerError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from error
    typer.echo("Shared server stopped." if stopped else "Shared server is not running.")
