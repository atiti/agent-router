from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .audit import AuditStore
from .classifier import (
    OpenAICompatibleClassifier,
    catalog_age_seconds,
    is_loopback_endpoint,
    is_private_endpoint,
    read_api_key,
)
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
        f"({decision.confidence:.0%} "
        f"{'classifier' if decision.classifier_confidence is not None else 'rule'} confidence)"
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
        f"Confidence: {row['confidence']:.0%}; rule score: {row['raw_score']:g}; "
        f"classifier: {row['classifier_version']}"
    )
    console.print(f"Classification source: {row['classification_source']}")
    if row["classifier_confidence"] is not None:
        console.print(
            f"LLM confidence: {row['classifier_confidence']:.0%}; "
            f"task type: {row['classifier_task_type']}"
        )
    if row["comparison_tier"]:
        console.print(
            f"Compared with: {row['comparison_tier']}; proposed: {row['proposed_tier']}; "
            f"task context: {'yes' if row['task_context_used'] else 'no'}"
        )
    if row["agent_requested_tier"]:
        console.print(f"Approved agent request: {row['agent_requested_tier']}")
    if row["selection_receipt_hash"]:
        console.print(f"Selection receipt: {row['selection_receipt_hash']}")
    for item in json.loads(row["contributions"]):
        sign = "+" if item["weight"] > 0 else ""
        console.print(f"  {sign}{item['weight']:g} {item['code']}: {item['detail']}")


@app.command("history")
def history_command(session: str | None = None, limit: int = 20) -> None:
    """Show recent routing decisions."""
    table = Table("ID", "Time", "Session", "Route", "Model", "Source", "Confidence", "Reasons")
    for row in AuditStore().history(session, limit):
        reasons = ", ".join(json.loads(row["reason_codes"]))
        table.add_row(
            str(row["id"]),
            row["created_at"][11:19],
            row["session_id"][:8],
            f"{row['comparison_tier'] or row['current_tier']} → {row['selected_tier']}",
            row["model"],
            row["classification_source"],
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
    sources: dict[str, int] = {}
    for row in automatic:
        source = str(row["classification_source"])
        sources[source] = sources.get(source, 0) + 1
    console.print("Classification sources:")
    for source, count in sorted(sources.items()):
        console.print(f"  {source}: {count}")
    labeled = [row for row in automatic if row["outcome_label"]]
    console.print(f"Labeled automatic decisions: {len(labeled)}")
    counts: dict[str, int] = {}
    for row in labeled:
        label = str(row["outcome_label"])
        counts[label] = counts.get(label, 0) + 1
    for label, count in sorted(counts.items()):
        console.print(f"  {label}: {count}")


@app.command("classifier-status")
def classifier_status_command() -> None:
    """Show classifier backend, privacy gate, and credential readiness."""
    routing = load_config().routing
    classifier = routing.classifier
    backend = (
        "local"
        if is_loopback_endpoint(classifier.endpoint)
        else "private"
        if is_private_endpoint(classifier.endpoint)
        else "cloud"
    )
    if backend == "local":
        credential_detail = "not required"
    else:
        try:
            credential = bool(read_api_key(classifier))
            credential_detail = "available" if credential else "missing"
        except (OSError, RuntimeError) as error:
            credential_detail = str(error)
    console.print(f"Mode: {routing.mode}")
    console.print(f"Classifier: {'enabled' if classifier.enabled else 'disabled'} ({backend})")
    console.print(f"Model: {classifier.model}")
    console.print(f"Endpoint: {classifier.endpoint}")
    console.print(f"Remote prompt egress: {'allowed' if classifier.allow_remote else 'blocked'}")
    credential_source = classifier.api_key_file or classifier.api_key_env
    console.print(f"Credential {credential_source}: {credential_detail}")
    console.print(
        f"Ambiguity threshold: {classifier.ambiguity_threshold:.0%}; "
        f"timeout: {classifier.timeout_seconds:g}s"
    )
    age = catalog_age_seconds(classifier)
    catalog_status = (
        "unverified"
        if age is None
        else "fresh"
        if age <= classifier.catalog_ttl_seconds
        else "stale"
    )
    console.print(
        f"Catalog: {catalog_status}; checked: {classifier.catalog_checked_at or 'never'}; "
        f"models: {len(classifier.catalog_models)}"
    )


@app.command("classifier-enable")
def classifier_enable_command(
    endpoint: str = typer.Option(
        "https://api.openai.com/v1/chat/completions", help="OpenAI-compatible endpoint."
    ),
    model: str = typer.Option("gpt-5-mini", help="Classifier model name."),
    api_key_env: str = typer.Option(
        "AGENTROUTE_CLASSIFIER_API_KEY", help="Environment variable containing the API key."
    ),
    api_key_file: Path | None = typer.Option(
        None, help="Private (chmod 600) file containing the API key."
    ),
    allow_remote: bool = typer.Option(
        False, "--allow-remote", help="Allow sending bounded task context to a remote endpoint."
    ),
    allow_private_http: bool = typer.Option(
        False,
        "--allow-private-http",
        help="Allow HTTP only when the endpoint host is a private or Tailscale IP.",
    ),
    timeout_seconds: float = typer.Option(
        5.0, "--timeout", min=0.1, max=30, help="Classifier request timeout in seconds."
    ),
    ambiguity_threshold: float = typer.Option(
        0.80,
        "--ambiguity-threshold",
        min=0.0,
        max=1.0,
        help="Use the LLM below this deterministic confidence.",
    ),
    reasoning_effort: str = typer.Option(
        "low", help="Reasoning effort sent to compatible classifier models."
    ),
) -> None:
    """Enable ambiguity-only LLM classification without storing credentials."""
    if not is_loopback_endpoint(endpoint) and not allow_remote:
        raise typer.BadParameter("remote endpoints require --allow-remote")
    if endpoint.startswith("http://") and not is_loopback_endpoint(endpoint):
        if not allow_private_http or not is_private_endpoint(endpoint):
            raise typer.BadParameter(
                "remote HTTP requires a private/Tailscale IP and --allow-private-http"
            )
    config = load_config()
    config.routing.mode = "hybrid"
    config.routing.classifier.enabled = True
    config.routing.classifier.endpoint = endpoint
    config.routing.classifier.model = model
    config.routing.classifier.api_key_env = api_key_env
    config.routing.classifier.api_key_file = (
        str(api_key_file.expanduser()) if api_key_file is not None else None
    )
    config.routing.classifier.allow_remote = allow_remote
    config.routing.classifier.allow_private_http = allow_private_http
    config.routing.classifier.timeout_seconds = timeout_seconds
    config.routing.classifier.ambiguity_threshold = ambiguity_threshold
    config.routing.classifier.reasoning_effort = reasoning_effort or None
    config.routing.classifier.catalog_checked_at = None
    config.routing.classifier.catalog_hash = None
    config.routing.classifier.catalog_models = []
    OpenAICompatibleClassifier(config.routing.classifier)
    save_config(config)
    console.print(f"Enabled hybrid classification with {model}.")
    if not is_loopback_endpoint(endpoint) and not api_key_file and not os.environ.get(api_key_env):
        console.print(
            f"Set {api_key_env} before launching Codex; heuristics remain the fallback."
        )


@app.command("classifier-verify")
def classifier_verify_command() -> None:
    """Verify the configured model against the provider catalog and cache the receipt."""
    config = load_config()
    classifier = OpenAICompatibleClassifier(config.routing.classifier)
    models, digest, checked_at = classifier.verify_catalog()
    config.routing.classifier.catalog_models = models
    config.routing.classifier.catalog_hash = digest
    config.routing.classifier.catalog_checked_at = checked_at
    save_config(config)
    console.print(
        f"Verified {config.routing.classifier.model} in {len(models)} visible models; "
        f"catalog {digest[:12]}."
    )


@app.command("classifier-refresh")
def classifier_refresh_command() -> None:
    """Refresh a missing or stale remote catalog; otherwise return immediately."""
    config = load_config()
    classifier_config = config.routing.classifier
    if not classifier_config.enabled or is_loopback_endpoint(classifier_config.endpoint):
        return
    age = catalog_age_seconds(classifier_config)
    if age is not None and age <= classifier_config.catalog_ttl_seconds:
        return
    classifier = OpenAICompatibleClassifier(classifier_config)
    models, digest, checked_at = classifier.verify_catalog()
    classifier_config.catalog_models = models
    classifier_config.catalog_hash = digest
    classifier_config.catalog_checked_at = checked_at
    save_config(config)


@app.command("classifier-disable")
def classifier_disable_command() -> None:
    """Disable LLM classification and retain deterministic routing."""
    config = load_config()
    config.routing.classifier.enabled = False
    save_config(config)
    console.print("LLM classification disabled; deterministic routing remains active.")


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
