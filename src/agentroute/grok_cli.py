"""Grok subscription bridge lifecycle, independent of the Claude service."""

from __future__ import annotations

import json
import os
import platform
import plistlib
import shlex
import shutil
import subprocess
from pathlib import Path

import typer

from .bridge_service import _agentroute_executable, wait_for_health
from .config import agentroute_home, load_config, save_config
from .grok_bridge import DEFAULT_PORT, GrokCredential, GrokServer, ensure_backend
from .providers import sync_codex_providers

app = typer.Typer(no_args_is_help=True, help="Route through your signed-in Grok CLI subscription.")
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


@app.command()
def serve(port: int = DEFAULT_PORT, grok_binary: str = "grok"):
    """Serve the subscription proxy on loopback."""
    GrokServer(port, GrokCredential(grok_binary)).serve_forever()


@app.command()
def install(port: int = DEFAULT_PORT):
    """Register @grok, install a user service, and check health."""
    if not 1 <= port <= 65535:
        raise typer.BadParameter("port must be between 1 and 65535")
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
