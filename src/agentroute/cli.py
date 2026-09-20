from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .analytics import usage_analytics
from .audit import AuditStore
from .capacity import (
    backend_spend,
    backend_state,
    fallback_chain,
    reset_description,
)
from .classifier import (
    OpenAICompatibleClassifier,
    catalog_age_seconds,
    is_loopback_endpoint,
    is_private_endpoint,
    read_api_key,
)
from .codex_patch import apply_patch, build_codex, install_binary
from .config import (
    SubscriptionProfileConfig,
    config_path,
    default_config,
    load_config,
    model_capabilities,
    save_config,
)
from .desktop import (
    DEFAULT_DESTINATION_APP,
    DEFAULT_SOURCE_APP,
    build_desktop_app,
    desktop_status,
    rollback_desktop_app,
)
from .doctor import run_doctor
from .hook import codex_stop, codex_user_prompt_submit
from .install import hook_command as installed_hook_command
from .install import merge_codex_hook, trust_agentroute_hooks
from .launcher import launch_codex
from .models import RouteContext, Tier
from .pricing import cost_report
from .profiles import bootstrap_profile_home, probe_profiles, routed_codex_binary
from .providers import (
    backend_readiness,
    effective_review_model,
    import_backend_credential,
    sync_codex_providers,
)
from .release import install_latest_release
from .router import Router

app = typer.Typer(no_args_is_help=True, help="Local, auditable model routing for coding agents.")
desktop_app = typer.Typer(no_args_is_help=True, help="Build and manage Codex Desktop locally.")
capacity_app = typer.Typer(no_args_is_help=True, help="Manage quota, budgets, and profiles.")
app.add_typer(desktop_app, name="desktop")
app.add_typer(capacity_app, name="capacity")
console = Console()


@app.command(
    "launch-codex",
    hidden=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def launch_codex_command(
    ctx: typer.Context,
    binary: Path | None = typer.Option(None, help="Routed Codex executable."),
    profile: str | None = typer.Option(None, help="Named isolated ChatGPT profile."),
) -> None:
    """Start Codex on the configured default backend before the first turn."""
    launch_codex(binary, ctx.args, profile)


def _format_duration(milliseconds: float | None) -> str:
    if milliseconds is None:
        return "—"
    seconds = milliseconds / 1000
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    if seconds >= 60:
        return f"{seconds / 60:.1f} min"
    if seconds >= 1:
        return f"{seconds:.1f} s"
    return f"{milliseconds:.0f} ms"


@capacity_app.command("enable")
def capacity_enable_command(
    warn_percent: float = typer.Option(85, min=0, max=100),
    switch_percent: float = typer.Option(95, min=0, max=100),
    recovery_margin_percent: float = typer.Option(20, min=0, max=100),
) -> None:
    """Enable quota and API-budget routing guardrails."""
    if warn_percent > switch_percent:
        raise typer.BadParameter("--warn-percent cannot exceed --switch-percent")
    config = load_config()
    config.capacity.enabled = True
    config.capacity.warn_percent = warn_percent
    config.capacity.switch_percent = switch_percent
    config.capacity.recovery_margin_percent = recovery_margin_percent
    save_config(config)
    console.print(
        "Capacity management enabled. Automatic routes may fail over between ready "
        "backends; explicit/sticky routes fail closed."
    )


@capacity_app.command("disable")
def capacity_disable_command() -> None:
    """Disable automatic quota and budget enforcement."""
    config = load_config()
    config.capacity.enabled = False
    save_config(config)
    console.print("Capacity management disabled; routing behavior is unchanged by quota state.")


@capacity_app.command("budget")
def capacity_budget_command(
    backend: str,
    daily: float | None = typer.Option(None, min=0.01, help="Daily USD limit."),
    monthly: float | None = typer.Option(None, min=0.01, help="Monthly USD limit."),
    clear_daily: bool = typer.Option(False, help="Remove the daily limit."),
    clear_monthly: bool = typer.Option(False, help="Remove the monthly limit."),
) -> None:
    """Set audited API-equivalent spend limits for one backend."""
    config = load_config()
    backend = backend.lower()
    if backend not in config.backends or backend == "gpt":
        raise typer.BadParameter("budgets apply to configured API backends, not gpt")
    target = config.backends[backend]
    if daily is not None:
        target.daily_budget_usd = daily
    if monthly is not None:
        target.monthly_budget_usd = monthly
    if clear_daily:
        target.daily_budget_usd = None
    if clear_monthly:
        target.monthly_budget_usd = None
    save_config(config)
    console.print(
        f"{backend} budget: daily "
        f"{target.daily_budget_usd if target.daily_budget_usd is not None else 'unlimited'}; "
        "monthly "
        f"{target.monthly_budget_usd if target.monthly_budget_usd is not None else 'unlimited'} "
        "USD."
    )


@capacity_app.command("fallback")
def capacity_fallback_command(
    backend: str,
    fallback: str | None = typer.Argument(None),
) -> None:
    """Set or clear a backend's next automatic fallback."""
    config = load_config()
    backend = backend.lower()
    fallback = fallback.lower() if fallback else None
    if backend not in config.backends:
        raise typer.BadParameter(f"unknown backend: {backend}")
    if fallback is not None and fallback not in config.backends:
        raise typer.BadParameter(f"unknown fallback backend: {fallback}")
    if fallback == backend:
        raise typer.BadParameter("a backend cannot fall back to itself")
    config.backends[backend].fallback_backend = fallback
    save_config(config)
    console.print(
        f"{backend} fallback: {fallback or 'none'}. "
        "Preference rings are supported; each backend is tried at most once per turn."
    )


@capacity_app.command("profile-add")
def capacity_profile_add_command(
    name: str,
    codex_home: Path,
    priority: int = typer.Option(100),
    select: bool = typer.Option(False, "--select", help="Use for future launches."),
) -> None:
    """Register an isolated CODEX_HOME without reading or copying credentials."""
    config = load_config()
    name = name.strip().lower()
    if not name:
        raise typer.BadParameter("profile name cannot be empty")
    config.capacity.profiles[name] = SubscriptionProfileConfig(
        codex_home=str(codex_home.expanduser()), priority=priority
    )
    if select or config.capacity.active_profile is None:
        config.capacity.active_profile = name
    save_config(config)
    console.print(
        f"Added profile {name} → {codex_home.expanduser()}. "
        "Sign in inside that CODEX_HOME, then relaunch Codex."
    )


@capacity_app.command("profile-remove")
def capacity_profile_remove_command(name: str) -> None:
    """Remove a profile reference; its CODEX_HOME and credentials are untouched."""
    config = load_config()
    name = name.lower()
    if name not in config.capacity.profiles:
        raise typer.BadParameter(f"unknown subscription profile: {name}")
    del config.capacity.profiles[name]
    if config.capacity.active_profile == name:
        config.capacity.active_profile = None
    save_config(config)
    console.print(f"Removed profile {name}; its files were not deleted.")


@capacity_app.command("profile-select")
def capacity_profile_select_command(name: str) -> None:
    """Select the profile used by future CLI and Desktop launches."""
    config = load_config()
    name = name.lower()
    if name not in config.capacity.profiles:
        raise typer.BadParameter(f"unknown subscription profile: {name}")
    config.capacity.active_profile = name
    save_config(config)
    console.print(
        f"Selected {name}. Existing sessions keep their current account; checkpoint and "
        "relaunch Codex to apply the profile."
    )


@capacity_app.command("profile-bootstrap")
def capacity_profile_bootstrap_command(
    name: str,
    source: Path = typer.Option(
        Path.home() / ".codex",
        "--from",
        help="Existing Codex home that supplies shared setup.",
    ),
) -> None:
    """Provision reusable hooks, MCP configuration, rules, and skills for one profile."""
    config = load_config()
    name = name.lower()
    profile = config.capacity.profiles.get(name)
    if profile is None:
        raise typer.BadParameter(f"unknown subscription profile: {name}")
    destination = Path(profile.codex_home).expanduser()
    try:
        copied = bootstrap_profile_home(source, destination)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    providers_path, _ = sync_codex_providers(config, destination / "config.toml")
    hooks_path, _ = merge_codex_hook(destination / "hooks.json")
    trusted = trust_agentroute_hooks(
        routed_codex_binary(), hooks_path=hooks_path, codex_home=destination
    )
    console.print(
        f"Bootstrapped {name}: {', '.join(copied) or 'no reusable files'}; "
        f"synced providers in {providers_path}; trusted {trusted} AgentRoute hooks. "
        "Authentication, sessions, history, plugins, and OAuth state were not copied."
    )


@capacity_app.command("status")
def capacity_status_command(
    json_output: bool = typer.Option(False, "--json"),
    no_probe: bool = typer.Option(False, help="Skip app-server profile quota reads."),
) -> None:
    """Show subscription quota, API budgets, fallbacks, and profile readiness."""
    config = load_config()
    rows = AuditStore().rows_since()
    daily, monthly = backend_spend(rows, config)
    profiles = () if no_probe else probe_profiles(config)
    active_profile = next(
        (
            profile
            for profile in profiles
            if profile.name == config.capacity.active_profile
        ),
        None,
    )
    backend_rows = []
    for name, backend in config.backends.items():
        state = (
            active_profile.capacity
            if name == "gpt" and active_profile is not None
            else backend_state(
                config,
                name,
                daily_spend=daily.get(name, 0.0),
                monthly_spend=monthly.get(name, 0.0),
            )
        )
        ready, problems = backend_readiness(config, name)
        backend_rows.append(
            {
                "name": name,
                "ready": ready,
                "readiness_detail": ", ".join(problems) or "ready",
                "status": state.status,
                "detail": state.detail,
                "daily_spend_usd": daily.get(name, 0.0),
                "daily_budget_usd": backend.daily_budget_usd,
                "monthly_spend_usd": monthly.get(name, 0.0),
                "monthly_budget_usd": backend.monthly_budget_usd,
                "fallback_chain": fallback_chain(config, name),
            }
        )
    payload = {
        "enabled": config.capacity.enabled,
        "warn_percent": config.capacity.warn_percent,
        "switch_percent": config.capacity.switch_percent,
        "recovery_margin_percent": config.capacity.recovery_margin_percent,
        "active_profile": config.capacity.active_profile,
        "backends": backend_rows,
        "profiles": [item.as_dict() for item in profiles],
    }
    if json_output:
        console.print_json(json.dumps(payload, sort_keys=True))
        return
    mode = "enabled" if config.capacity.enabled else "disabled"
    console.print(
        f"[bold]Capacity management: {mode}[/bold] · warn {config.capacity.warn_percent:g}% "
        f"· switch {config.capacity.switch_percent:g}% · recovery margin "
        f"{config.capacity.recovery_margin_percent:g}%"
    )
    table = Table("Backend", "Ready", "Capacity", "Today", "Month", "Fallback")
    for item in backend_rows:
        table.add_row(
            item["name"],
            "yes" if item["ready"] else item["readiness_detail"],
            f"{item['status']}: {item['detail']}",
            f"${item['daily_spend_usd']:.2f} / "
            f"{item['daily_budget_usd'] if item['daily_budget_usd'] else '∞'}",
            f"${item['monthly_spend_usd']:.2f} / "
            f"{item['monthly_budget_usd'] if item['monthly_budget_usd'] else '∞'}",
            " → ".join(item["fallback_chain"]) or "—",
        )
    console.print(table)
    if no_probe and config.capacity.profiles:
        console.print("Profile probes skipped; run without --no-probe for live quota state.")
    elif profiles:
        profile_table = Table("Profile", "Active", "Signed in", "Account", "Capacity", "Reset")
        for item in profiles:
            profile_table.add_row(
                item.name,
                "yes" if item.name == config.capacity.active_profile else "",
                "yes" if item.authenticated else "no",
                item.account_hash or "—",
                f"{item.capacity.status}: {item.capacity.detail}",
                reset_description(item.capacity.resets_at) or "—",
            )
        console.print(profile_table)
    if config.capacity.profiles:
        console.print(
            "Profile changes apply only at launch. Existing sessions keep their current account "
            "to preserve provider state; checkpoint and relaunch to switch subscriptions."
        )


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


@app.command("setup")
def setup_command() -> None:
    """Initialize, enable, and connect an installed AgentRoute runtime."""
    path = config_path()
    if path.exists():
        config = load_config()
        config.enabled = True
    else:
        config = default_config()
        config.enabled = True
    save_config(config, path)
    hooks_path, backup = merge_codex_hook()
    providers_path, providers_backup = sync_codex_providers(config)
    console.print(f"✓ Config enabled: {path}")
    console.print(f"✓ Codex hooks connected: {hooks_path}")
    console.print(f"✓ Provider config synchronized: {providers_path}")
    if backup:
        console.print(f"  Hooks backup: {backup}")
    if providers_backup:
        console.print(f"  Provider backup: {providers_backup}")
    routed = Path(os.environ.get("AGENTROUTE_HOME", str(Path.home() / ".agentroute")))
    routed /= "bin/codex-bin"
    if not routed.exists():
        console.print(
            "[yellow]Patched Codex binary is missing; install a release or build from source."
            "[/yellow]"
        )
    else:
        console.print("✓ Routed Codex runtime is installed")
        try:
            trusted_count = trust_agentroute_hooks(routed, hooks_path=hooks_path)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            console.print(f"[red]Could not trust AgentRoute hooks: {error}[/red]")
            raise typer.Exit(1) from error
        console.print(
            f"✓ Trusted {trusted_count} exact AgentRoute hooks for interactive and exec use"
        )


@app.command("update")
def update_command(
    repository: str | None = typer.Option(
        None, help="GitHub owner/repository; defaults to the official AgentRoute repository."
    ),
    allow_downgrade: bool = typer.Option(
        False,
        "--allow-downgrade",
        help="Explicitly permit installing an older published version.",
    ),
) -> None:
    """Download, checksum, and install the latest prebuilt AgentRoute release."""
    console.print("Downloading the latest release for this platform...")
    try:
        version = install_latest_release(repository, allow_downgrade=allow_downgrade)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        console.print(f"[red]Update failed: {error}[/red]")
        raise typer.Exit(1) from error
    console.print(f"AgentRoute {version} installed. Restart active Codex sessions.")


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


@app.command("models")
def models_command() -> None:
    """Show configured answer models and their declared agent capabilities."""
    config = load_config()
    table = Table(
        "Backend", "Tier", "Model", "Tools", "Reasoning", "Vision", "Context", "Price / 1M"
    )
    for backend_name, backend in sorted(config.backends.items()):
        for tier, target in backend.tiers.items():
            capabilities = model_capabilities(config, backend_name, target.model)
            pricing_model = capabilities.pricing_model or target.model
            resolved_price_model = config.pricing.aliases.get(pricing_model, pricing_model)
            price = config.pricing.models.get(resolved_price_model)
            table.add_row(
                backend_name,
                tier.upper(),
                target.model,
                capabilities.tool_calling,
                "yes"
                if capabilities.reasoning
                else "no"
                if capabilities.reasoning is False
                else "unknown",
                "yes"
                if capabilities.vision
                else "no"
                if capabilities.vision is False
                else "unknown",
                f"{capabilities.context_window:,}" if capabilities.context_window else "unknown",
                (
                    f"{config.pricing.currency} {price.input_per_million:g} in / "
                    f"{price.output_per_million:g} out"
                    if price
                    else "unpriced"
                ),
            )
    console.print(table)


@app.command("doctor")
def doctor_command(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable results."),
) -> None:
    """Validate the local runtime, hooks, providers, classifier, audit store, and capabilities."""
    try:
        checks = run_doctor(load_config())
    except Exception as error:
        console.print(
            f"[red]Doctor could not load configuration: "
            f"{type(error).__name__}: {error}[/red]"
        )
        raise typer.Exit(1) from error
    if json_output:
        console.print_json(json.dumps([asdict(check) for check in checks], sort_keys=True))
    else:
        table = Table("Status", "Check", "Detail")
        labels = {
            "pass": "[green]PASS[/green]",
            "warn": "[yellow]WARN[/yellow]",
            "fail": "[red]FAIL[/red]",
        }
        for check in checks:
            table.add_row(labels[check.status], check.name, check.detail)
        console.print(table)
        passed = sum(check.status == "pass" for check in checks)
        warnings = sum(check.status == "warn" for check in checks)
        failures = sum(check.status == "fail" for check in checks)
        console.print(f"{passed} passed; {warnings} warnings; {failures} failures")
    if any(check.status == "fail" for check in checks):
        raise typer.Exit(1)


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


@app.command("analytics")
def analytics_command(
    days: int = typer.Option(30, min=1, help="Rolling UTC window to include."),
    all_time: bool = typer.Option(False, "--all", help="Include all local audit history."),
    bucket: str = typer.Option("day", help="Timeline bucket: day, week, or month."),
    baseline: str | None = typer.Option(None, help="Fixed model used for comparison."),
    session: str | None = typer.Option(None, help="Restrict to one local session ID."),
    longest: int = typer.Option(10, min=0, help="Number of longest completed turns to show."),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable local analytics."
    ),
) -> None:
    """Break down local model and classifier usage, including a UTC timeline."""
    if bucket not in {"day", "week", "month"}:
        raise typer.BadParameter("bucket must be one of: day, week, month")
    config = load_config()
    baseline = baseline or config.pricing.baseline_model
    if baseline not in config.pricing.models and baseline not in config.pricing.aliases:
        raise typer.BadParameter(f"no configured price for baseline model: {baseline}")
    since = None if all_time else datetime.now(timezone.utc) - timedelta(days=days)
    rows = AuditStore().rows_since(since=since, session_id=session)
    report = usage_analytics(rows, config.pricing, baseline, bucket=bucket, longest=longest)
    currency = config.pricing.currency
    if json_output:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "since": since.isoformat() if since else None,
            "bucket": bucket,
            "baseline": baseline,
            "currency": currency,
            "rows": report.rows,
            "model_mismatch_turns": report.model_mismatch_turns,
            "overall": asdict(report.overall)
            | {
                "gross_savings": report.overall.gross_savings,
                "net_savings": report.overall.net_savings,
                "savings_percent": report.overall.savings_percent,
            },
            "by_model": [asdict(item) for item in report.by_model],
            "over_time": [asdict(item) for item in report.over_time],
            "classifiers": [asdict(item) for item in report.classifiers],
            "duration": asdict(report.duration),
            "reconciliation": asdict(report.reconciliation),
            "capacity": asdict(report.capacity),
            "longest_turns": [asdict(item) for item in report.longest_turns],
        }
        console.print_json(json.dumps(payload, sort_keys=True))
        return
    if not rows:
        window = "all time" if all_time else f"the last {days} day(s)"
        console.print(f"No local routing decisions in {window}.")
        return

    console.print(
        f"[bold]Local usage ({'all time' if all_time else f'last {days} day(s)'}, UTC)[/bold]"
    )
    console.print(
        f"Turns: {report.rows}; token receipts: {report.overall.measured_turns}; "
        f"without token receipt: {report.overall.unmeasured_turns}"
    )
    console.print(
        f"Completed: {report.duration.completed_turns}; "
        f"duration avg {_format_duration(report.duration.average_ms)}; "
        f"p50 {_format_duration(report.duration.p50_ms)}; "
        f"p95 {_format_duration(report.duration.p95_ms)}; "
        f"max {_format_duration(report.duration.maximum_ms)}"
    )
    reconciliation = report.reconciliation
    console.print(
        "Reconciliation: "
        f"metered {reconciliation.completed_metered}; "
        f"completed without receipt {reconciliation.completed_unmetered}; "
        f"pending {reconciliation.pending}; stale {reconciliation.stale_unreconciled}; "
        f"failed {reconciliation.failed}; interrupted {reconciliation.interrupted}"
    )
    capacity = report.capacity
    console.print(
        "Capacity: "
        f"warnings {capacity.warnings}; fallbacks {capacity.fallbacks}; "
        f"blocked {capacity.blocked}; fallback runtime "
        f"{_format_duration(capacity.fallback_duration_ms)}"
    )
    if capacity.by_route:
        console.print(
            "Fallback routes: "
            + ", ".join(f"{route}={count}" for route, count in capacity.by_route.items())
        )
    console.print(
        f"Routed answer cost: {currency} {report.overall.actual_cost:.4f}; "
        f"fixed {baseline}: {currency} {report.overall.baseline_cost:.4f}; "
        f"classifier overhead: {currency} {report.overall.classifier_cost:.4f}; "
        f"net savings: {currency} {report.overall.net_savings:.4f} "
        f"({report.overall.savings_percent:.1f}%)"
    )

    models = Table(
        "Backend",
        "Answer model",
        "Turns",
        "Completed",
        "Token receipts",
        "Tokens",
        "Answer cost",
    )
    for item in report.by_model:
        models.add_row(
            item.backend,
            item.model,
            str(item.turns),
            str(item.completed_turns),
            str(item.measured_turns),
            f"{item.total_tokens:,}",
            f"{currency} {item.answer_cost:.4f}",
        )
    console.print(models)

    durations = Table(
        "Backend", "Answer model", "Completed", "Avg", "P50", "P95", "Max"
    )
    for item in report.by_model:
        if not item.completed_turns:
            continue
        durations.add_row(
            item.backend,
            item.model,
            str(item.completed_turns),
            _format_duration(item.average_duration_ms),
            _format_duration(item.p50_duration_ms),
            _format_duration(item.p95_duration_ms),
            _format_duration(item.maximum_duration_ms),
        )
    console.print(durations)

    timeline = Table(
        "Period (UTC)",
        "Backend",
        "Answer model",
        "Turns",
        "Avg time",
        "Tokens",
        "Answer cost",
    )
    for item in report.over_time:
        timeline.add_row(
            item.period,
            item.backend,
            item.model,
            str(item.turns),
            _format_duration(item.average_duration_ms),
            f"{item.total_tokens:,}",
            f"{currency} {item.answer_cost:.4f}",
        )
    console.print(timeline)

    if report.classifiers:
        classifiers = Table(
            "Classifier model", "Calls", "OK", "Fallback", "Timeout", "Error", "Tokens",
            "Avg", "P50", "P95", "Estimated cost", "Failure reasons"
        )
        for item in report.classifiers:
            classifiers.add_row(
                item.model,
                str(item.calls),
                f"{item.successful_calls} ({item.success_rate:.1%})",
                f"{item.fallback_calls} ({item.fallback_rate:.1%})",
                f"{item.timeout_calls} ({item.timeout_rate:.1%})",
                str(item.error_calls),
                f"{item.total_tokens:,}",
                _format_duration(item.average_latency_ms),
                _format_duration(item.p50_latency_ms),
                _format_duration(item.p95_latency_ms),
                f"{currency} {item.estimated_cost:.4f}",
                ", ".join(f"{reason}={count}" for reason, count in item.failure_reasons.items())
                or "—",
            )
        console.print(classifiers)
    if report.longest_turns:
        longest_table = Table("ID", "Started (UTC)", "Route", "Tier", "Duration", "Tokens")
        for item in report.longest_turns:
            longest_table.add_row(
                str(item.decision_id),
                item.created_at[:19].replace("T", " "),
                f"{item.backend}/{item.model}",
                item.tier,
                _format_duration(item.duration_ms),
                f"{item.total_tokens:,}",
            )
        console.print(longest_table)
    if report.model_mismatch_turns:
        console.print(
            f"[yellow]Normalized {report.model_mismatch_turns} historical Stop-hook model "
            "receipts that reported the frozen session-start model instead of the routed model."
            "[/yellow]"
        )
    if report.overall.unpriced_models:
        console.print(
            "[yellow]Unpriced models excluded from cost estimates: "
            + ", ".join(report.overall.unpriced_models)
            + ". Add rates or aliases under pricing.models/pricing.aliases.[/yellow]"
        )
    console.print(
        "Costs are API-equivalent estimates from observed token counters, "
        "not a subscription invoice."
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


@app.command("backend-default")
def backend_default_command(
    backend: str = typer.Argument(..., help="Enabled backend: gpt, azure, or deepseek"),
) -> None:
    """Route every intelligence tier through one ready backend by default."""
    config = load_config()
    backend = backend.lower()
    if backend not in config.backends or not config.backends[backend].enabled:
        raise typer.BadParameter(f"backend is not enabled: {backend}")
    ready, problems = backend_readiness(config, backend)
    if not ready:
        raise typer.BadParameter(
            f"backend is not ready: {backend} ({', '.join(problems)})"
        )
    for tier in ("fast", "normal", "smart", "max"):
        config.routing.backend_by_tier[tier] = backend
    save_config(config)
    console.print(f"All tiers now default to {backend}.")
    console.print(
        f"Start Codex with `codex`; inside Codex, `@{backend} PROMPT` is an explicit override."
    )


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
    console.print(
        "Change every tier with `agentroute backend-default BACKEND`; "
        "provider prefixes such as `@azure` belong inside a Codex prompt, not the shell."
    )


@app.command("backend-sync")
def backend_sync_command() -> None:
    """Synchronize enabled non-secret providers into Codex configuration."""
    path, backup = sync_codex_providers(load_config())
    console.print(f"Synced {path}")
    if backup:
        console.print(f"Backup: {backup}")


@desktop_app.command("status")
def desktop_status_command(
    source: Path = typer.Option(DEFAULT_SOURCE_APP, help="Official ChatGPT.app path."),
    destination: Path = typer.Option(DEFAULT_DESTINATION_APP, help="Routed app path."),
) -> None:
    """Show official, routed, and embedded Codex compatibility versions."""
    status = desktop_status(source.expanduser(), destination.expanduser())
    for key, value in status.items():
        console.print(f"{key.replace('_', ' ').title()}: {value}")


@desktop_app.command("install")
def desktop_install_command(
    source: Path = typer.Option(DEFAULT_SOURCE_APP, help="Official ChatGPT.app path."),
    destination: Path = typer.Option(DEFAULT_DESTINATION_APP, help="Routed app path."),
    signing_identity: str = typer.Option(
        "-", help="Apple signing identity; '-' creates a local ad-hoc signature."
    ),
    allow_version_mismatch: bool = typer.Option(
        False,
        help="Bypass the app-server compatibility guard (mobile remote may reject it).",
    ),
) -> None:
    """Create a routed app from the locally installed official ChatGPT app."""
    try:
        installed, _ = build_desktop_app(
            source.expanduser(),
            destination.expanduser(),
            signing_identity=signing_identity,
            allow_version_mismatch=allow_version_mismatch,
        )
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        console.print(f"[red]Desktop install failed: {error}[/red]")
        raise typer.Exit(1) from error
    console.print(f"Created {installed}")
    console.print("The official app was not modified. Quit it before opening the routed copy.")


@desktop_app.command("rebuild")
def desktop_rebuild_command(
    source: Path = typer.Option(DEFAULT_SOURCE_APP, help="Official ChatGPT.app path."),
    destination: Path = typer.Option(DEFAULT_DESTINATION_APP, help="Routed app path."),
    signing_identity: str = typer.Option(
        "-", help="Apple signing identity; '-' creates a local ad-hoc signature."
    ),
    allow_version_mismatch: bool = typer.Option(
        False,
        help="Bypass the app-server compatibility guard (mobile remote may reject it).",
    ),
) -> None:
    """Rebuild from the latest official app while preserving a rollback copy."""
    try:
        installed, backup = build_desktop_app(
            source.expanduser(),
            destination.expanduser(),
            signing_identity=signing_identity,
            replace=True,
            allow_version_mismatch=allow_version_mismatch,
        )
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        console.print(f"[red]Desktop rebuild failed: {error}[/red]")
        raise typer.Exit(1) from error
    console.print(f"Rebuilt {installed}")
    if backup:
        console.print(f"Rollback copy: {backup}")


@desktop_app.command("rollback")
def desktop_rollback_command(
    destination: Path = typer.Option(DEFAULT_DESTINATION_APP, help="Routed app path."),
) -> None:
    """Restore the newest routed Desktop backup."""
    try:
        restored, replaced = rollback_desktop_app(destination.expanduser())
    except (OSError, RuntimeError) as error:
        console.print(f"[red]Desktop rollback failed: {error}[/red]")
        raise typer.Exit(1) from error
    console.print(f"Restored {restored}")
    console.print(f"Replaced app retained at {replaced}")


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
            "SubagentStop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": installed_hook_command("stop"),
                            "statusMessage": "AgentRoute is recording subagent token usage",
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
