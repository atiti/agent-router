"""Explicit controls and previews for context experiments."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from .config import codex_home, load_config, save_config
from .context import related_memories
from .efficiency import execution_receipt

app = typer.Typer(no_args_is_help=True, help="Preview existing Codex memory references.")


@app.command("search")
def search(
    query: str,
    cwd: Path = typer.Option(Path.cwd()),
    session: str = typer.Option("", help="Exclude this current thread."),
) -> None:
    """Read-only JSON preview; works without enabling automatic context."""
    config = load_config()
    result = related_memories(codex_home() / "memories", query, str(cwd), session, config.context)
    typer.echo(json.dumps(result, indent=2))


@app.command("mode")
def mode(value: str) -> None:
    """off: disabled; shadow: audit only; references: bounded hook source pointers."""
    if value not in {"off", "shadow", "references"}:
        raise typer.BadParameter("expected off, shadow, or references")
    config = load_config()
    config.context.mode = value
    save_config(config)
    typer.echo(f"Related Codex context: {value}. Applies on the next routed turn.")


@app.command("economics")
def economics(value: str) -> None:
    """Configure cache estimates: off, shadow, or retain (API backends only)."""
    if value not in {"off", "shadow", "retain"}:
        raise typer.BadParameter("expected off, shadow, or retain")
    config = load_config()
    config.routing.switching.cache_economics = value
    save_config(config)
    typer.echo(f"Cache economics: {value}. Estimates concern the next request only.")


@app.command("execution")
def execution(transcript: Path, turn: str) -> None:
    """Inspect exact-turn execution receipts without writing the audit database."""
    typer.echo(json.dumps(execution_receipt(str(transcript), turn), indent=2))
