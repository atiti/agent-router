"""Grok subscription bridge lifecycle, independent of the Claude service."""

from __future__ import annotations

import json
import os
import platform
import plistlib
import secrets
import shlex
import shutil
import subprocess
from enum import Enum
from pathlib import Path

import typer

from .bridge_service import _agentroute_executable, wait_for_health
from .config import agentroute_home, load_config, save_config
from .grok_bridge import (
    BRIDGE_KEY_ENV,
    BRIDGE_KEY_PREFIX,
    DEFAULT_PORT,
    GrokApiCredential,
    GrokCredential,
    GrokServer,
    MissingCredential,
    api_credentials_path,
    ensure_backend,
    read_managed_credential,
)
from .providers import credentials_path, import_backend_credential, sync_codex_providers

app = typer.Typer(
    no_args_is_help=True, help="Route through your Grok CLI subscription or public xAI API key."
)
LABEL = "com.agentroute.grok-bridge"
UNIT = "agentroute-grok-bridge.service"


def unit_path():
    if platform.system() == "Darwin":
        return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    return Path.home() / ".config/systemd/user" / UNIT


def service_definition(binary, port):
    args = [
        str(_agentroute_executable()),
        "grok",
        "serve",
        "--port",
        str(port),
        "--grok-binary",
        binary,
    ]
    environment = {
        key: os.environ[key]
        for key in ("GROK_HOME", "GROK_AUTH_PATH", "AGENTROUTE_HOME")
        if key in os.environ
    }
    if platform.system() == "Darwin":
        return plistlib.dumps(
            {
                "Label": LABEL,
                "ProgramArguments": args,
                "EnvironmentVariables": environment,
                "RunAtLoad": True,
                "KeepAlive": True,
                "StandardOutPath": str(agentroute_home() / "grok-bridge.log"),
                "StandardErrorPath": str(agentroute_home() / "grok-bridge.log"),
            }
        )
    # systemd command lines use double quotes, not shell single quotes.
    command = " ".join(
        '"'
        + a.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        + '"'
        for a in args
    )
    environment_lines = "".join(
        'Environment="'
        + key
        + "="
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
        + '"\n'
        for key, value in environment.items()
    )
    return (
        "[Unit]\nDescription=AgentRoute Grok subscription bridge\nAfter=network-online.target\n"
        f"[Service]\n{environment_lines}ExecStart={command}\nRestart=always\nRestartSec=3\n"
        "[Install]\nWantedBy=default.target\n"
    ).encode()


def run(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True)


class CredentialMode(str, Enum):
    subscription = "subscription"
    api_key = "api-key"


@app.command()
def serve(
    port: int = DEFAULT_PORT,
    grok_binary: str = "grok",
    credential: CredentialMode = CredentialMode.subscription,
    instance: str = "",
):
    """Serve the selected credential mode on loopback; subscription is the default."""
    source = (
        GrokApiCredential() if credential == CredentialMode.api_key else GrokCredential(grok_binary)
    )
    GrokServer(port, source, instance).serve_forever()


@app.command("credential-import")
def credential_import(source_env: str):
    """Store an API key from an environment variable, without printing it."""
    value = os.environ.get(source_env)
    if not value or any(not 33 <= ord(char) <= 126 for char in value):
        raise typer.BadParameter("API credential must be nonempty ASCII without whitespace")
    config = load_config()
    ensure_backend(config, credential_mode="api-key")
    config.backends["grok"].api_key_env = "XAI_API_KEY"
    if api_credentials_path().resolve() == credentials_path().resolve():
        raise typer.BadParameter("The upstream API key file must differ from the client key file")
    path, name = import_backend_credential(config, "grok", source_env, api_credentials_path())
    ensure_client_credential(config)
    typer.echo(f"Stored {name} in {path}; the API bridge reads rotations automatically.")


def ensure_client_credential(config):
    try:
        existing = read_managed_credential(BRIDGE_KEY_ENV)
        if existing.startswith(BRIDGE_KEY_PREFIX):
            return
    except (FileNotFoundError, MissingCredential):
        existing = secrets.token_urlsafe(32)
    config = config.model_copy(deep=True)
    config.backends["grok"].api_key_env = BRIDGE_KEY_ENV
    source = "AGENTROUTE_GROK_BRIDGE_IMPORT"
    previous = os.environ.get(source)
    try:
        os.environ[source] = BRIDGE_KEY_PREFIX + existing
        import_backend_credential(config, "grok", source)
    finally:
        if previous is None:
            os.environ.pop(source, None)
        else:
            os.environ[source] = previous




@app.command()
def install(port: int = DEFAULT_PORT):
    """Register @grok, install a user service, and check health."""
    if not 1 <= port <= 65535:
        raise typer.BadParameter("port must be between 1 and 65535")
    path = unit_path()
    if path.exists():
        if platform.system() == "Darwin":
            arguments = plistlib.loads(path.read_bytes()).get("ProgramArguments", [])
        else:
            command = next(
                row for row in path.read_text().splitlines() if row.startswith("ExecStart=")
            )
            arguments = shlex.split(command.removeprefix("ExecStart="))
        if "api-key" in arguments:
            raise typer.BadParameter("Stop the API service before installing subscription mode")
    binary = shutil.which("grok")
    if not binary:
        raise typer.BadParameter("Install Grok CLI and run grok login first")
    GrokCredential(binary).token()
    config = load_config()
    ensure_backend(config, port)
    save_config(config)
    sync_codex_providers(config)
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(service_definition(binary, port))
    if platform.system() == "Darwin":
        run("launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}", check=False)
        # Reuse the retry behavior from the existing Claude service lifecycle.
        import time

        for _attempt in range(6):
            result = run("launchctl", "bootstrap", f"gui/{os.getuid()}", str(path), check=False)
            if result.returncode == 0:
                break
            time.sleep(0.4)
        else:
            raise RuntimeError("Cannot start Grok LaunchAgent; check launchctl")
    else:
        run("systemctl", "--user", "daemon-reload")
        run("systemctl", "--user", "enable", UNIT)
        run("systemctl", "--user", "restart", UNIT)
    health = wait_for_health(port)
    if health.status != "healthy" or (health.payload or {}).get("provider") != "grok":
        raise RuntimeError(f"Grok service did not become healthy: {health.detail}")
    typer.echo(f"Grok subscription ready on port {port}. Use @grok in Codex.")


@app.command()
def status():
    """Report service configuration and health without using subscription allowance."""
    path = unit_path()
    if not path.exists():
        typer.echo("Grok service not installed; run agentroute grok install")
        raise typer.Exit(1)
    if platform.system() == "Darwin":
        args = plistlib.loads(path.read_bytes())["ProgramArguments"]
    else:
        line = next(row for row in path.read_text().splitlines() if row.startswith("ExecStart="))
        args = shlex.split(line.removeprefix("ExecStart="))
    port = int(args[args.index("--port") + 1])
    health = wait_for_health(port, timeout=2)
    typer.echo(
        json.dumps({"service": str(path), "status": health.status, "health": health.payload})
    )
    if health.status != "healthy" or (health.payload or {}).get("provider") != "grok":
        raise typer.Exit(1)


@app.command()
def uninstall():
    """Stop and remove only the Grok service and disable its backend."""
    path = unit_path()
    if platform.system() == "Darwin":
        run("launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}", check=False)
    else:
        run("systemctl", "--user", "disable", "--now", UNIT, check=False)
    path.unlink(missing_ok=True)
    config = load_config()
    if "grok" in config.backends:
        config.backends["grok"].enabled = False
        save_config(config)
        sync_codex_providers(config)
    typer.echo("Grok bridge removed.")
