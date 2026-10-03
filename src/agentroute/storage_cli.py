"""Inspect storage and explicitly remove disposable caches."""

import json

import typer

from .config import agentroute_home
from .storage import file_sizes, prune_build_cache, prune_desktop_backups, prune_diagnostics

storage_app = typer.Typer(no_args_is_help=True, help="Inspect storage and prune diagnostic caches.")


@storage_app.command("status")
def status() -> None:
    home = agentroute_home()
    typer.echo(
        json.dumps(
            {
                "home": str(home),
                "bytes": {p.name: file_sizes(p)[0] for p in home.iterdir() if not p.is_symlink()},
            },
            indent=2,
        )
    )


@storage_app.command("prune")
def prune(
    build_cache: bool = typer.Option(False, help="Remove inactive generated Rust outputs."),
    desktop_backups: bool = typer.Option(
        False, help="Keep only the newest two routed app backups."
    ),
) -> None:
    """Prune diagnostics. Chats, source edits, configuration and credentials are preserved."""
    try:
        result = dict(prune_diagnostics())
        if build_cache:
            result["build_cache"] = {"removed_bytes": prune_build_cache()}
        if desktop_backups:
            result["desktop_backups"] = {"removed_bytes": prune_desktop_backups()}
        typer.echo(json.dumps(result, indent=2))
    except (OSError, ValueError, RuntimeError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from error
