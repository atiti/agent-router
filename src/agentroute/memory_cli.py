"""Memory inspection, bounded spike capture, and an explicit Playwright guard installer."""

import json
from pathlib import Path

import typer

from .memory_profile import default_output, snapshot, watch
from .playwright_memory import install_guard

memory_app = typer.Typer(no_args_is_help=True, help="Inspect runtime memory and capture spikes.")


@memory_app.command("status")
def status() -> None:
    typer.echo(json.dumps(snapshot(), indent=2))


@memory_app.command("watch")
def watch_command(
    duration: float = typer.Option(3600, help="Bounded observation duration in seconds."),
    interval: float = typer.Option(10, help="Sampling interval in seconds."),
    threshold_mib: int = typer.Option(
        4096, help="Codex footprint that triggers a memory-map summary."
    ),
    output: Path | None = typer.Option(None, help="Private local output directory."),
) -> None:
    try:
        result = watch(
            duration=duration,
            interval=interval,
            threshold_bytes=threshold_mib * 1024**2,
            output=output or default_output(),
        )
    except (ValueError, OSError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(json.dumps(result, indent=2))


@memory_app.command("playwright-install")
def playwright_install(original: Path | None = typer.Option(None)) -> None:
    try:
        typer.echo(json.dumps(install_guard(original), indent=2))
    except (ValueError, OSError) as error:
        raise typer.BadParameter(str(error)) from error
