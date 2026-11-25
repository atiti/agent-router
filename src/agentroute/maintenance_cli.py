"""Install and inspect the per-user maintenance schedule."""

from __future__ import annotations

import json
import os
import platform
import plistlib
import subprocess
from pathlib import Path

import typer

from .bridge_service import _agentroute_executable
from .config import agentroute_home
from .dashboard_decisions import register_receipt_command

LABEL = "com.agentroute.maintenance"
INTERVAL = 60
maintenance_app = typer.Typer(
    no_args_is_help=True, help="Inspect and schedule AgentRoute maintenance."
)


def service_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def payload() -> dict:
    log = agentroute_home() / "maintenance.log"
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(_agentroute_executable()), "maintenance", "run", "--apply", "--summary",
        ],
        "RunAtLoad": True,
        "StartInterval": INTERVAL,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "PATH": os.pathsep.join([
                str(agentroute_home() / "bin"), str(Path.home() / ".local" / "bin"),
                "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
            ]),
        },
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], text=True, capture_output=True, timeout=20)


@maintenance_app.command("install")
def install() -> None:
    """Install the per-user macOS schedule and start the first tick."""
    if platform.system() != "Darwin":
        raise typer.BadParameter("Automatic installation is currently available on macOS only.")
    path = service_path()
    if path.is_symlink():
        raise typer.BadParameter("Refusing to replace a symlinked service file.")
    desired = payload()
    if path.exists():
        try:
            current = plistlib.loads(path.read_bytes())
        except (ValueError, OSError) as error:
            raise typer.BadParameter(
                "Existing service file is unreadable; inspect it first."
            ) from error
        # Only an exact managed definition is safe to reuse. Never overwrite another job.
        if current != desired:
            raise typer.BadParameter(
                f"Existing {path} differs from the expected service; inspect it."
            )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        agentroute_home().mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            plistlib.dump(desired, stream, sort_keys=False)
    domain = f"gui/{os.getuid()}"
    if _launchctl("print", f"{domain}/{LABEL}").returncode:
        result = _launchctl("bootstrap", domain, str(path))
        if result.returncode:
            raise typer.BadParameter(f"Service saved at {path}, but launchctl could not load it: "
                                     f"{result.stderr.strip() or result.stdout.strip()}")
    typer.echo(f"Maintenance schedule installed at {path}. Run maintenance status for results.")


@maintenance_app.command("run")
def run(
    apply: bool = typer.Option(False, "--apply", help="Perform eligible maintenance actions."),
    summary: bool = typer.Option(False, "--summary", help="Print a compact tick receipt."),
) -> None:
    """Inspect maintenance work, optionally applying eligible recovery actions."""
    from . import maintenance

    result = maintenance.run_once(apply=apply)
    if summary and "packets" in result:
        result["packets"] = {k: v for k, v in result["packets"].items() if k != "items"}
    typer.echo(json.dumps(result, indent=None if summary else 2, default=str))


@maintenance_app.command("status")
def status() -> None:
    """Show the last maintenance tick and the state of its login schedule."""
    from . import maintenance

    path = maintenance.status_path()
    try:
        last_run = json.loads(path.read_text())
    except (OSError, ValueError):
        last_run = None
    loaded = None
    if platform.system() == "Darwin":
        try:
            loaded = _launchctl("print", f"gui/{os.getuid()}/{LABEL}").returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    typer.echo(json.dumps({"schedule_file": str(service_path()),
                           "schedule_installed": service_path().is_file(),
                           "schedule_loaded": loaded, "last_run": last_run}, indent=2))


register_receipt_command(maintenance_app)
