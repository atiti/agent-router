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
from .hook import codex_stop, codex_user_prompt_submit
from .install import hook_command as installed_hook_command
from .install import merge_codex_hook
from .models import RouteContext, Tier
from .pricing import cost_report
from .providers import (
    backend_readiness,
    effective_review_model,
    import_backend_credential,
    sync_codex_providers,
)
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
        f"via {decision.backend}/{decision.model_provider} "
        f"({decision.confidence:.0%} "
        f"{'classifier' if decision.classifier_confidence is not None else 'rule'} confidence)"
    )
    for item in decision.contributions:
        sign = "+" if item.weight > 0 else ""
        console.print(f"  {sign}{item.weight:g} {item.code.value}: {item.detail}")


@app.command("hook")
def hook_command(provider: str, event: str) -> None:
    """Run a provider hook protocol over stdin/stdout."""
    handlers = {
        ("codex", "user-prompt-submit"): codex_user_prompt_submit,
        ("codex", "stop"): codex_stop,
    }
    handler = handlers.get((provider, event))
    if handler is None:
        raise typer.BadParameter("supported hooks: codex user-prompt-submit, codex stop")
    raise typer.Exit(handler())


@app.command("why")
def why_command(session: str | None = None) -> None:
    """Explain the latest recorded routing decision."""
    row = AuditStore().latest(session)
    if row is None:
        console.print("No routing decisions recorded.")
        raise typer.Exit(1)
    console.print(
        f"[bold]{row['selected_tier'].upper()}[/bold] → {row['model']} "
        f"via {row['backend']}/{row['model_provider']} ({row['route_scope']})"
    )
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
    if row["classifier_latency_ms"] is not None:
        usage = json.loads(row["classifier_usage"] or "{}")
        console.print(
            f"Classifier latency: {row['classifier_latency_ms']:.1f} ms; "
            f"usage: {json.dumps(usage, sort_keys=True)}"
        )
    if row["comparison_tier"]:
        console.print(
            f"Compared with: {row['comparison_tier']}; proposed: {row['proposed_tier']}; "
            f"previous context sent: {'yes' if row['previous_context_sent'] else 'no'}; "
            f"task inherited: {'yes' if row['resolved_task_inherited'] else 'no'}"
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
    table = Table(
        "ID", "Time", "Scope", "Session", "Route", "Backend", "Model", "Source",
        "Confidence", "Reasons"
    )
    for row in AuditStore().history(session, limit):
        reasons = ", ".join(json.loads(row["reason_codes"]))
        table.add_row(
            str(row["id"]),
            row["created_at"][11:19],
            row["route_scope"],
            row["session_id"][:8],
            f"{row['comparison_tier'] or row['current_tier']} → {row['selected_tier']}",
            row["backend"],
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
    llm = [
        row
        for row in automatic
        if row["classification_source"] in {"local_llm", "private_llm", "cloud_llm"}
    ]
    latencies = [float(row["classifier_latency_ms"]) for row in llm if row["classifier_latency_ms"]]
    if latencies:
        console.print(
            f"LLM latency: avg {sum(latencies) / len(latencies):.0f} ms; "
            f"min {min(latencies):.0f}; max {max(latencies):.0f}"
        )
    usage_totals: dict[str, int | float] = {}
    for row in llm:
        for name, value in json.loads(row["classifier_usage"] or "{}").items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage_totals[name] = usage_totals.get(name, 0) + value
    if usage_totals:
        console.print(f"LLM usage totals: {json.dumps(usage_totals, sort_keys=True)}")


@app.command("stats")
def stats_command(
    baseline: str | None = typer.Option(None, help="Fixed model used for comparison."),
    limit: int = typer.Option(500, min=1, help="Most recent routing decisions to include."),
) -> None:
    """Estimate routed cost and savings from measured per-turn token usage."""
    config = load_config()
    baseline = baseline or config.pricing.baseline_model
    if baseline not in config.pricing.models and baseline not in config.pricing.aliases:
        raise typer.BadParameter(f"no configured price for baseline model: {baseline}")
    rows = AuditStore().history(limit=limit)
    report = cost_report(rows, config.pricing, baseline)
    currency = config.pricing.currency
    console.print(
        f"Measured turns: {report.measured_turns}; "
        f"awaiting/unmeasured: {report.unmeasured_turns}"
    )
    console.print(
        "Answer tokens: "
        f"input {report.input_tokens:,}; cached {report.cached_input_tokens:,}; "
        f"cache write {report.cache_write_input_tokens:,}; output {report.output_tokens:,}; "
        f"reasoning output {report.reasoning_output_tokens:,}"
    )
    console.print(f"Routed answer cost: {currency} {report.actual_cost:.4f}")
    console.print(f"Fixed {baseline} baseline: {currency} {report.baseline_cost:.4f}")
    console.print(f"Classifier overhead: {currency} {report.classifier_cost:.4f}")
    if report.unpriced_models:
        console.print(
            "[yellow]Unpriced models excluded from routed cost: "
            + ", ".join(report.unpriced_models)
            + ". Add rates or aliases under pricing.models/pricing.aliases.[/yellow]"
        )
    console.print(
        f"Net estimated savings: {currency} {report.net_savings:.4f} "
        f"({report.savings_percent:.1f}%)"
    )
    console.print(
        "Estimate uses observed routed-turn token counts at configured API rates; "
        "it is not a Codex subscription invoice or a prediction of another model's token count."
    )


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


@app.command("backend-enable")
def backend_enable_command(
    name: str = typer.Argument(..., help="Backend name: gpt, azure, or deepseek."),
    base_url: str | None = typer.Option(None, help="Responses API base URL."),
    fast_model: str | None = typer.Option(None),
    normal_model: str | None = typer.Option(None),
    smart_model: str | None = typer.Option(None),
    max_model: str | None = typer.Option(None),
    review_model: str | None = typer.Option(
        None,
        help="Dedicated automatic-approval model; defaults to this backend's FAST model.",
    ),
    api_key_header: str | None = typer.Option(
        None,
        help="Credential header: api-key for direct Azure, authorization for bearer proxies.",
    ),
) -> None:
    """Enable an execution backend and sync its non-secret Codex provider config."""
    config = load_config()
    name = name.lower()
    if name not in config.backends:
        raise typer.BadParameter(f"unknown backend: {name}")
    backend = config.backends[name]
    if base_url:
        backend.base_url = base_url.rstrip("/")
    if name == "azure" and not backend.base_url:
        raise typer.BadParameter("Azure requires --base-url ending in /openai/v1")
    if api_key_header:
        normalized_header = api_key_header.lower()
        if normalized_header not in {"api-key", "authorization"}:
            raise typer.BadParameter("--api-key-header must be api-key or authorization")
        backend.api_key_header = normalized_header
    for tier, model in {
        "fast": fast_model,
        "normal": normal_model,
        "smart": smart_model,
        "max": max_model,
    }.items():
        if model:
            backend.tiers[tier].model = model
    if review_model:
        backend.review_model = review_model
    backend.enabled = True
    save_config(config)
    path, backup = sync_codex_providers(config)
    console.print(f"Enabled {name}; synced {path}")
    if backup:
        console.print(f"Backup: {backup}")
    ready, problems = backend_readiness(config, name)
    if not ready:
        missing_credentials = [item for item in problems if item.startswith("missing ")]
        if missing_credentials:
            console.print(
                f"Import {backend.api_key_env} before launching Codex with "
                f"`agentroute backend-credential-import {name} SOURCE_ENV`."
            )


@app.command("backend-disable")
def backend_disable_command(name: str) -> None:
    """Disable an API backend and remove its generated Codex provider entry."""
    config = load_config()
    name = name.lower()
    if name == "gpt":
        raise typer.BadParameter("the ChatGPT subscription backend cannot be disabled")
    if name not in config.backends:
        raise typer.BadParameter(f"unknown backend: {name}")
    config.backends[name].enabled = False
    for tier, selected in list(config.routing.backend_by_tier.items()):
        if selected == name:
            config.routing.backend_by_tier[tier] = "gpt"
    save_config(config)
    path, _ = sync_codex_providers(config)
    console.print(f"Disabled {name}; synced {path}")


@app.command("backend-credential-import")
def backend_credential_import_command(
    name: str = typer.Argument(..., help="Backend name: azure or deepseek."),
    source_env: str = typer.Argument(..., help="Environment variable to import from."),
) -> None:
    """Store one backend credential in AgentRoute's owner-only local credential file."""
    config = load_config()
    name = name.lower()
    if name not in config.backends or name == "gpt":
        raise typer.BadParameter(f"backend does not accept an API credential: {name}")
    try:
        path, target_env = import_backend_credential(config, name, source_env)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    console.print(f"Stored {target_env} for {name} in owner-only file {path}; value not shown.")


@app.command("backend-route")
def backend_route_command(
    tier: str = typer.Argument(..., help="fast, normal, smart, or max"),
    backend: str = typer.Argument(..., help="gpt, azure, or deepseek"),
) -> None:
    """Choose the default execution backend for one intelligence tier."""
    config = load_config()
    parsed_tier = str(Tier.parse(tier))
    backend = backend.lower()
    if backend not in config.backends or not config.backends[backend].enabled:
        raise typer.BadParameter(f"backend is not enabled: {backend}")
    config.routing.backend_by_tier[parsed_tier] = backend
    save_config(config)
    console.print(f"{parsed_tier.upper()} now routes through {backend}.")


@app.command("backend-status")
def backend_status_command() -> None:
    """Show execution-provider mappings without printing credentials."""
    config = load_config()
    table = Table("Backend", "State", "Codex provider", "Endpoint", "Reviewer", "Models")
    for name, backend in config.backends.items():
        ready, problems = backend_readiness(config, name)
        state = "ready" if ready else ", ".join(problems)
        models = ", ".join(
            f"{tier}={target.model}" for tier, target in backend.tiers.items()
        )
        table.add_row(
            name,
            state,
            backend.codex_provider,
            backend.base_url or "ChatGPT subscription",
            effective_review_model(config, name),
            models,
        )
    console.print(table)
    console.print(
        "Defaults: "
        + ", ".join(
            f"{tier}={backend}" for tier, backend in config.routing.backend_by_tier.items()
        )
    )


@app.command("backend-sync")
def backend_sync_command() -> None:
    """Synchronize enabled non-secret providers into Codex configuration."""
    path, backup = sync_codex_providers(load_config())
    console.print(f"Synced {path}")
    if backup:
        console.print(f"Backup: {backup}")


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
    command = installed_hook_command("user-prompt-submit")
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
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": installed_hook_command("stop"),
                            "statusMessage": "AgentRoute is recording token usage",
                        }
                    ]
                }
            ],
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
