"""Local capture controls and measured usage/context reports."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .capacity import local_time_description
from .config import load_config
from .profiling import capture_status, profile_report, set_capture

app = typer.Typer(no_args_is_help=True, help="Profile provider usage and assembled model context.")


@app.command("on")
def enable() -> None:
    """Capture new Codex sessions and Claude bridge requests locally."""
    set_capture(True)
    typer.echo("Profiling enabled. Start a new Codex session through AgentRoute.")
    typer.echo(
        "Raw Codex traces retain request/response text locally in " + capture_status()["root"]
    )
    typer.echo("Reports contain counts and sizes. No profiling data is uploaded.")


@app.command("off")
def disable() -> None:
    """Stop capture for new sessions; retain existing records."""
    set_capture(False)
    typer.echo(
        "Profiling disabled. Runtime v50 stops managed capture during existing sessions.\n"
        "Restart older runtimes to stop their capture."
    )
    typer.echo(
        "Existing local records are retained; explicit capture environment variables still apply."
    )


@app.command("prune")
def prune() -> None:
    """Remove old diagnostics: raw traces 512 MiB / 7 days; receipts 64 MiB / 30 days."""
    from .storage import prune_diagnostics

    typer.echo(json.dumps(prune_diagnostics(), indent=2))


@app.command("status")
def status() -> None:
    typer.echo(json.dumps(capture_status(), indent=2))


@app.command("report")
def report(
    days: float = typer.Option(1, min=0.001, max=3650, help="Look back this many days."),
    session: str = typer.Option("", help="Filter a root session or child thread ID."),
    turn: str = typer.Option("", help="Filter a Codex turn ID."),
    root: Path | None = typer.Option(None, help="Read an alternative profiling directory."),
    json_output: bool = typer.Option(
        False, "--json", help="Print the complete counts-only report."
    ),
    output: Path | None = typer.Option(None, help="Save the complete report as private JSON."),
    rollout: Path | None = typer.Option(
        None, help="Include tokens from an existing Codex rollout."
    ),
) -> None:
    """Show costs, token usage, context composition, and repeated request exposure."""
    data = profile_report(
        load_config().pricing, root=root, days=days, session=session, turn=turn, rollout=rollout
    )
    if output:
        # The report is counts-only, but correlation IDs are still local session data.
        output.write_text(json.dumps(data, indent=2), encoding="utf-8")
        output.chmod(0o600)
    if json_output:
        typer.echo(json.dumps(data, indent=2))
        return
    console = Console()
    console.print(
        f"{data['requests_count']:,} requests; {data['measured_requests']:,} usage receipts; "
        f"{data['unpriced_requests']:,} unpriced; {data['read_errors']:,} read errors",
        markup=False,
    )
    if not data["requests_count"]:
        console.print("No captured requests in this range. Use profile on and start a new session.")
        return
    console.print(
        f"{data['classifier_requests']:,} recorded routing classifier calls; "
        f"{data['requests_count'] - data['classifier_requests']:,} model/compaction attempts"
    )
    table = Table(title="Measured tokens and API-equivalent cost estimates")
    table.add_column("Component")
    table.add_column("Tokens", justify="right")
    table.add_column("USD estimate", justify="right")
    for label, token_key, cost_keys in (
        ("Fresh input", "uncached_input_tokens", ("uncached_input",)),
        ("Cache reads", "cached_input_tokens", ("cache_read",)),
        (
            "Cache writes",
            "cache_write_input_tokens",
            ("cache_write_5m", "cache_write_1h", "cache_write_unknown_ttl"),
        ),
        ("Output (includes reasoning)", "output_tokens", ("output",)),
    ):
        cost = sum(data["cost_components_usd"].get(key, 0) for key in cost_keys)
        table.add_row(label, f"{data['tokens'][token_key]:,}", f"${cost:.4f}")
    table.add_row("Priced total", "", f"${data['priced_usd']:.4f}")
    console.print(table)
    models = Table(title="Models")
    for name in ("Model", "Requests", "Input incl. cache", "Output", "USD estimate"):
        models.add_column(name)
    for row in sorted(data["models"], key=lambda r: r["priced_usd"], reverse=True):
        models.add_row(
            row["model"],
            str(row["requests"]),
            f"{row['tokens']['input_tokens']:,}",
            f"{row['tokens']['output_tokens']:,}",
            (
                "unknown"
                if row["unpriced_requests"] == row["requests"]
                else f"${row['priced_usd']:.4f}"
                + (" (partial)" if row["unpriced_requests"] else "")
            ),
        )
    console.print(models)
    threads = Table(title="Threads and context growth (classifier input excluded)")
    for name in ("Thread", "Agent", "Requests", "First / last / peak input", "USD estimate"):
        threads.add_column(name)
    for row in sorted(data["threads"], key=lambda r: r["priced_usd"], reverse=True)[:10]:
        threads.add_row(
            row["thread_id"],
            row["agent_path"] or "unknown",
            str(row["requests"]),
            f"{row['first_input_tokens']} / {row['last_input_tokens']} / "
            f"{row['max_input_tokens']:,}",
            f"${row['priced_usd']:.4f}",
        )
    if any(row["first_input_tokens"] is not None for row in data["threads"]):
        console.print(threads)
    largest = data["largest_context_request"] or {}
    context = largest.get("context", {})
    usage = largest.get("usage") or {}
    if largest:
        console.print(
            f"Largest captured context input: {usage.get('input_tokens', 0):,} tokens; "
            f"model {largest.get('model')}; thread {largest.get('thread_id')}; "
            f"turn {largest.get('turn_id')}",
            markup=False,
        )
    else:
        console.print("No assembled request captures yet. Start a new routed Codex session.")
    if largest.get("context_window"):
        console.print(
            f"Provider input / advertised window: "
            f"{usage.get('input_tokens', 0) / largest['context_window']:.1%}"
        )
    components = Table(title="Context sections (bytes measured; tokens estimated)")
    for name in (
        "Section",
        "Largest request bytes",
        "Est. tokens",
        "Bytes sent across all requests",
    ):
        components.add_column(name)
    for key in sorted(
        data["context_exposure_bytes"], key=data["context_exposure_bytes"].get, reverse=True
    ):
        part = context.get("components", {}).get(key, {})
        components.add_row(
            key,
            f"{part.get('bytes', 0):,}",
            f"{part.get('estimated_tokens', 0):,}",
            f"{data['context_exposure_bytes'][key]:,}",
        )
    if context.get("components"):
        console.print(components)
    if context.get("largest_named_items"):
        named = Table(title="Largest named content in that request")
        for name in ("Kind", "Skill / tool", "Bytes"):
            named.add_column(name)
        for row in context["largest_named_items"][:8]:
            named.add_row(row["category"], row["name"], f"{row['bytes']:,}")
        console.print(named)
    if any(context.get("media", {}).values()):
        console.print("Media / opaque provider state: " + json.dumps(context.get("media", {})))
    requests = Table(title="Largest input requests")
    for name in ("Timestamp", "Thread / turn", "Input", "USD estimate", "Outcome"):
        requests.add_column(name)
    for row in data["largest_requests"]:
        cost = row["cost"]["total_usd"]
        requests.add_row(
            row["timestamp"],
            f"{row.get('thread_id')} / {row.get('turn_id')}",
            f"{(row.get('usage') or {}).get('input_tokens', 0):,}",
            f"${cost:.4f}" if cost is not None else "unknown",
            row["outcome"],
        )
    console.print(requests)
    if data["subscription_observations"]:
        snapshots = Table(title="Observed account quota samples (shared across sessions)")
        for name in ("Sampled at (unix)", "Request at", "Window", "Used", "Resets at"):
            snapshots.add_column(name)
        for observation in data["subscription_observations"][-8:]:
            for name, window in observation["windows"].items():
                snapshots.add_row(
                    str(observation["timestamp"]),
                    observation["request_timestamp"],
                    name,
                    str(window.get("used_percent", "unknown")),
                    local_time_description(window.get("resets_at")) or "unknown",
                )
        console.print(snapshots)
    for note in data["notes"]:
        console.print(note, markup=False)
    if output:
        console.print(f"Saved {output}", markup=False)
