"""Operator commands for separate Claude Code subscription credential stores."""

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .capacity import local_time_description
from .claude_profiles import profile_directory, profile_status
from .config import ClaudeSubscriptionProfile, agentroute_home, load_config, save_config

app = typer.Typer(no_args_is_help=True, help="Manage isolated Claude subscriptions.")
console = Console()


@app.command("add")
def add(name: str, config_dir: Path | None = None, priority: int = 100) -> None:
    """Register a subscription; new accounts get their own Claude Code config directory."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name):
        raise typer.BadParameter("name must be lowercase letters, digits, or hyphens")
    config = load_config()
    if name in config.claude_subscriptions.profiles:
        raise typer.BadParameter(f"profile already exists: {name}")
    if len(config.claude_subscriptions.profiles) >= 20:
        raise typer.BadParameter("at most 20 Claude subscription profiles are supported")
    if priority < 0:
        raise typer.BadParameter("priority must be nonnegative")
    directory = (config_dir or agentroute_home() / "claude-accounts" / name).expanduser().absolute()
    if any(
        profile_directory(item).resolve() == directory.resolve()
        for item in config.claude_subscriptions.profiles.values()
    ):
        raise typer.BadParameter("this Claude credential store is already registered")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        directory.chmod(0o700)
    config.claude_subscriptions.profiles[name] = ClaudeSubscriptionProfile(
        config_dir=str(directory), priority=priority, auth_generation=uuid.uuid4().hex
    )
    save_config(config)
    console.print(f"Registered {name}. Sign in with `agentroute bridge profile login {name}`.")


@app.command("login")
def login(name: str) -> None:
    """Sign in through Claude Code's official interactive subscription login."""
    config = load_config()
    profile = config.claude_subscriptions.profiles.get(name)
    if profile is None or not profile.enabled:
        raise typer.BadParameter(f"profile is unknown or disabled: {name}")
    executable = shutil.which("claude")
    if not executable:
        raise typer.BadParameter("install Claude Code before signing in")
    env = dict(os.environ)
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR",
    ):
        env.pop(key, None)
    if profile.config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(profile_directory(profile))
    result = subprocess.run([executable, "auth", "login", "--claudeai"], env=env)
    if result.returncode:
        raise typer.Exit(result.returncode)
    profile.auth_generation = uuid.uuid4().hex
    save_config(config)
    usage = profile_status(config, name)
    if usage["error"]:
        console.print(f"Signed in {name}; live quota read unavailable: {usage['error']}")
    else:
        console.print(f"Signed in {name} and read its current subscription quota.")
    console.print(f"Use `agentroute bridge profile use {name}` as the default for new threads.")


@app.command("use")
def use(name: str) -> None:
    """Select the default profile for new Claude conversations."""
    config = load_config()
    profile = config.claude_subscriptions.profiles.get(name)
    if profile is None or not profile.enabled:
        raise typer.BadParameter(f"profile is unknown or disabled: {name}")
    config.claude_subscriptions.active_profile = name
    save_config(config)
    console.print(f"Claude default for new conversations: {name}.")


@app.command("remove")
def remove(name: str) -> None:
    """Unregister a profile while retaining its Claude Code credentials."""
    config = load_config()
    if name == "default" or name == config.claude_subscriptions.active_profile:
        raise typer.BadParameter("select another profile first; default cannot be removed")
    if name not in config.claude_subscriptions.profiles:
        raise typer.BadParameter(f"unknown profile: {name}")
    del config.claude_subscriptions.profiles[name]
    save_config(config)
    console.print(f"Unregistered {name}; its Claude Code credential store is retained.")


@app.command("status")
def status(
    offline: bool = False,
    json_output: bool = typer.Option(False, "--json"),
    profile: str | None = typer.Option(None, "--profile", help="Show only this account."),
) -> None:
    """Show each account's own usage; status reads never refresh OAuth credentials."""
    config = load_config()
    if profile and profile not in config.claude_subscriptions.profiles:
        raise typer.BadParameter(f"unknown profile: {profile}")
    names = [profile] if profile else list(config.claude_subscriptions.profiles)
    rows = [profile_status(config, name, offline=offline) for name in names]
    if json_output:
        console.print_json(json.dumps(rows))
        return
    table = Table("Profile", "Default", "Identity", "Usage source", "Limit", "Used", "Resets")
    for row in rows:
        for limit in row["limits"] or [{}]:
            percent = limit.get("percent")
            table.add_row(
                row["name"],
                "yes" if row["active"] else "",
                row["identity_status"],
                row["status"],
                str(limit.get("label") or row["error"] or "unavailable"),
                f"{percent:g}%" if isinstance(percent, (int, float)) else "—",
                local_time_description(limit.get("resets_at")) or "—",
            )
    console.print(table)
