from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .audit import AuditStore
from .codex_patch import apply_patch, build_codex, install_binary
from .config import config_path, default_config, load_config, save_config
from .hook import codex_user_prompt_submit
from .install import hook_command as installed_hook_command
from .install import merge_codex_hook
from .models import RouteContext, Tier
from .router import Router

app = typer.Typer(no_args_is_help=True, help="Local, auditable model routing for coding agents.")
console = Console()


@app.command("init")
def init_command(
    enable: bool = typer.Option(False, help="Enable native switching; requires patched Codex."),
    force: bool = typer.Option(False, help="Replace an existing AgentRoute config."),
) -> None:
    """Create a local configuration. Starts in observe mode by default."""
    path = config_path()
    if path.exists() and not force:
        console.print(f"Config already exists: {path}")
        raise typer.Exit(1)
    config = default_config()
    config.enabled = enable
    save_config(config, path)
    console.print(f"Created {path}")
    console.print("Mode: [bold]enabled[/bold]" if enable else "Mode: [bold]observe only[/bold]")


@app.command("enable")
def enable_command() -> None:
    """Enable model overrides in hook output."""
    config = load_config()
    config.enabled = True
    save_config(config)
    console.print("AgentRoute enabled. Use a patched Codex binary with step_model_switching.")


@app.command("observe")
def observe_command() -> None:
    """Record and show decisions without changing models."""
    config = load_config()
    config.enabled = False
    save_config(config)
    console.print("AgentRoute is observing only.")


@app.command("test")
def test_command(
    prompt: str,
    provider: str = "codex",
    current: str = "normal",
    previous: str | None = None,
) -> None:
    """Simulate a routing decision without writing audit state."""
    context = RouteContext(
        session_id="simulation",
        provider=provider,
        latest_prompt=prompt,
        current_tier=Tier.parse(current),
        previous_task_tier=Tier.parse(previous) if previous else None,
    )
    decision = Router().route(context)
    console.print(
        f"[bold]{decision.tier.name}[/bold] → {decision.model} "
        f"({decision.confidence:.0%} rule confidence)"
    )
    for item in decision.contributions:
        sign = "+" if item.weight > 0 else ""
        console.print(f"  {sign}{item.weight:g} {item.code.value}: {item.detail}")


@app.command("hook")
def hook_command(provider: str, event: str) -> None:
    """Run a provider hook protocol over stdin/stdout."""
    if (provider, event) != ("codex", "user-prompt-submit"):
        raise typer.BadParameter("supported hook: codex user-prompt-submit")
    raise typer.Exit(codex_user_prompt_submit())


@app.command("why")
def why_command(session: str | None = None) -> None:
    """Explain the latest recorded routing decision."""
    row = AuditStore().latest(session)
    if row is None:
        console.print("No routing decisions recorded.")
        raise typer.Exit(1)
    console.print(f"[bold]{row['selected_tier'].upper()}[/bold] → {row['model']}")
    console.print(
        f"Rule confidence: {row['confidence']:.0%}; raw score: {row['raw_score']:g}; "
        f"classifier: {row['classifier_version']}"
    )
    if row["comparison_tier"]:
        console.print(
            f"Compared with: {row['comparison_tier']}; proposed: {row['proposed_tier']}; "
            f"task context: {'yes' if row['task_context_used'] else 'no'}"
        )
    if row["agent_requested_tier"]:
        console.print(f"Approved agent request: {row['agent_requested_tier']}")
    for item in json.loads(row["contributions"]):
        sign = "+" if item["weight"] > 0 else ""
        console.print(f"  {sign}{item['weight']:g} {item['code']}: {item['detail']}")


@app.command("history")
def history_command(session: str | None = None, limit: int = 20) -> None:
    """Show recent routing decisions."""
    table = Table("ID", "Time", "Session", "Route", "Model", "Rule confidence", "Reasons")
    for row in AuditStore().history(session, limit):
        reasons = ", ".join(json.loads(row["reason_codes"]))
        table.add_row(
            str(row["id"]),
            row["created_at"][11:19],
            row["session_id"][:8],
            f"{row['comparison_tier'] or row['current_tier']} → {row['selected_tier']}",
            row["model"],
            f"{row['confidence']:.0%}",
            reasons,
        )
    console.print(table)


@app.command("label")
def label_command(
    decision_id: int,
    outcome: str,
    notes: str | None = None,
) -> None:
    """Label a routing outcome for later calibration."""
    allowed = {"correct", "too-low", "too-high", "overridden", "failed"}
    if outcome not in allowed:
        raise typer.BadParameter(f"outcome must be one of: {', '.join(sorted(allowed))}")
    if not AuditStore().label(decision_id, outcome, notes):
        console.print(f"No routing decision with ID {decision_id}.")
        raise typer.Exit(1)
    console.print(f"Labeled decision {decision_id}: {outcome}")


@app.command("audit-report")
def audit_report_command(limit: int = 500) -> None:
    """Summarize automatic routes and available quality labels."""
    rows = AuditStore().history(limit=limit)
    automatic = [row for row in rows if not row["manual_override"]]
    console.print(f"Automatic decisions: {len(automatic)} of {len(rows)}")
    for tier in ("fast", "normal", "smart", "max"):
        console.print(f"  {tier.upper()}: {sum(row['selected_tier'] == tier for row in automatic)}")
    labeled = [row for row in automatic if row["outcome_label"]]
    console.print(f"Labeled automatic decisions: {len(labeled)}")
    counts: dict[str, int] = {}
    for row in labeled:
        label = str(row["outcome_label"])
        counts[label] = counts.get(label, 0) + 1
    for label, count in sorted(counts.items()):
        console.print(f"  {label}: {count}")


@app.command("doctor")
def doctor_command() -> None:
    """Check local prerequisites and configuration."""
    checks = {
        "Codex": shutil.which("codex") or "missing",
        "Git": shutil.which("git") or "missing",
        "Cargo": shutil.which("cargo") or "missing",
        "Config": str(config_path()) if config_path().exists() else "not initialized",
    }
    for name, value in checks.items():
        console.print(f"{'✓' if value != 'missing' else '✗'} {name}: {value}")
    if shutil.which("codex"):
        result = subprocess.run(["codex", "--version"], capture_output=True, text=True)
        console.print(result.stdout.strip() or result.stderr.strip())


@app.command("codex-patch")
def codex_patch_command(
    source: Path = typer.Option(..., exists=True, file_okay=False, resolve_path=True),
    check: bool = typer.Option(False, help="Check applicability without changing the checkout."),
    build: bool = typer.Option(False, help="Build Codex after applying the patch."),
    install_to: Path | None = typer.Option(None, help="Copy built binary to this path."),
) -> None:
    """Apply the native per-turn routing patch to an OpenAI Codex checkout."""
    apply_patch(source, check=check)
    console.print(f"{'Patch applies cleanly' if check else 'Applied patch'} to {source}")
    if build and not check:
        binary = build_codex(source)
        console.print(f"Built {binary}")
        if install_to:
            console.print(f"Installed {install_binary(binary, install_to.expanduser())}")


@app.command("hook-json")
def hook_json_command() -> None:
    """Print the Codex hooks.json fragment for AgentRoute."""
    command = installed_hook_command()
    payload = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "statusMessage": "AgentRoute is selecting a model",
                        }
                    ]
                }
            ]
        }
    }
    console.print_json(data=payload)


@app.command("install-hook")
def install_hook_command(path: Path | None = None) -> None:
    """Merge the AgentRoute hook into Codex's hooks.json, with a backup."""
    installed, backup = merge_codex_hook(path)
    console.print(f"Installed AgentRoute hook in {installed}")
    if backup:
        console.print(f"Backup: {backup}")


if __name__ == "__main__":
    app()
