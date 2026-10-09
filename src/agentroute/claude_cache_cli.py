"""Configure bridge caching and inspect bounded numeric reuse receipts."""

import json
from enum import Enum

import typer
from rich.console import Console
from rich.table import Table

from .claude_cache_usage import cache_usage_report
from .config import load_config, save_config

app = typer.Typer(no_args_is_help=True, help="Configure and inspect Claude prompt caching.")


class CacheMode(str, Enum):
    auto = "auto"
    off = "off"


class CacheTTL(str, Enum):
    five_minutes = "5m"
    one_hour = "1h"


@app.command("configure")
def configure(mode: CacheMode, ttl: CacheTTL = CacheTTL.five_minutes) -> None:
    """Set auto/off and a 5m/1h TTL; takes effect on the next bridge request."""
    config = load_config()
    config.claude_cache.mode = mode.value
    config.claude_cache.ttl = ttl.value
    save_config(config)
    typer.echo(f"Claude prompt caching: {mode.value}; TTL {ttl.value}. Applies to new requests.")


@app.command("status")
def status(
    days: float = typer.Option(1, min=0.001, max=30),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show cache reads/writes by profile and model, without raw prompt capture."""
    report = cache_usage_report(days=days)
    report["settings"] = load_config().claude_cache.model_dump()
    if json_output:
        typer.echo(json.dumps(report, indent=2))
        return
    console = Console()
    console.print(
        f"Claude prompt caching: {report['settings']['mode']}; TTL {report['settings']['ttl']}"
    )
    console.print(
        f"{report['retained_requests']} retained requests in {days:g} days "
        f"(maximum {report['receipt_limit']}; 30-day retention)."
    )
    if report["error"]:
        console.print(report["error"])
    if not report["groups"]:
        console.print("No cache receipts yet. Run a Claude turn through the bridge.")
        return
    table = Table(title="Claude cache reuse")
    for label in (
        "Profile",
        "Model",
        "Read %",
        "Fresh",
        "Reads",
        "Writes 5m / 1h",
    ):
        table.add_column(label, overflow="fold")
    for group in report["groups"]:
        tokens = group["tokens"]
        percentage = group["cache_read_percent"]
        table.add_row(
            group["profile"],
            group["model"],
            f"{percentage:.1f}%" if percentage is not None else "unknown",
            f"{tokens['uncached_input_tokens']:,}",
            f"{tokens['cached_input_tokens']:,}",
            f"{tokens['cache_write_5m_input_tokens']:,} / "
            f"{tokens['cache_write_1h_input_tokens']:,}",
        )
    console.print(table)
    for group in report["groups"]:
        context = group["latest_context_bytes"]
        console.print(
            f"{group['profile']} / {group['model']}: "
            f"{group['measured_requests']} of {group['requests']} requests measured; "
            f"latest tools {context['tools_bytes'] / 1000:.1f} KB, "
            f"system {context['system_bytes'] / 1000:.1f} KB."
        )
    console.print("Cache reads still occupy context. Counts are not subscription quota debits.")
