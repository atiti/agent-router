"""Install and inspect the Claude bridge as a per-user background service.

The bridge has to be running whenever Codex routes a turn to Claude, so the
friendly path is a managed per-user service rather than a terminal that has to
stay open: a LaunchAgent on macOS, a `systemd --user` unit on Linux.
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import agentroute_home

SERVICE_LABEL = "com.agentroute.claude-bridge"
SYSTEMD_UNIT = "agentroute-claude-bridge.service"
DEFAULT_BRIDGE_PORT = 8090
DEFAULT_BRIDGE_HOST = "127.0.0.1"

# `launchctl bootout` returns before launchd has finished tearing the job down, and
# a `bootstrap` that lands in that window fails with a bare
# "Bootstrap failed: 5: Input/output error". Retry briefly before giving up.
BOOTSTRAP_ATTEMPTS = 6
BOOTSTRAP_RETRY_SECONDS = 0.4


class BridgeServiceError(RuntimeError):
    """Raised when the bridge service cannot be installed or controlled."""


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def service_unit_path() -> Path:
    """Where the service definition lives for this platform."""
    if platform.system() == "Darwin":
        return _launch_agent_path()
    return _systemd_unit_path()


def _agentroute_executable() -> Path:
    """Resolve the console script next to the running interpreter."""
    candidate = Path(sys.executable).parent / "agentroute"
    if candidate.exists():
        return candidate
    found = shutil.which("agentroute")
    if found:
        return Path(found)
    raise BridgeServiceError(
        "cannot find the `agentroute` executable next to the Python interpreter; "
        "install AgentRoute before installing the bridge service"
    )


def _launch_agent_payload(port: int, credential: str, host: str) -> dict:
    log_path = agentroute_home() / "bridge.log"
    return {
        "Label": SERVICE_LABEL,
        "ProgramArguments": [
            str(_agentroute_executable()),
            "bridge",
            "serve",
            "--credential",
            credential,
            "--host",
            host,
            "--port",
            str(port),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


def _systemd_unit_text(port: int, credential: str, host: str) -> str:
    return (
        "[Unit]\n"
        "Description=AgentRoute Claude bridge (Anthropic Messages to OpenAI Responses)\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={_agentroute_executable()} bridge serve "
        f"--credential {credential} --host {host} --port {port}\n"
        "Restart=always\n"
        "RestartSec=3\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True)


def write_service_unit(port: int, credential: str, host: str) -> Path:
    """Write the platform service definition and return its path."""
    path = service_unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        with path.open("wb") as handle:
            plistlib.dump(_launch_agent_payload(port, credential, host), handle, sort_keys=False)
    else:
        path.write_text(_systemd_unit_text(port, credential, host), encoding="utf-8")
    return path


def _load_service(path: Path) -> None:
    uid = os.getuid()
    if platform.system() == "Darwin":
        _run(["launchctl", "bootout", f"gui/{uid}/{SERVICE_LABEL}"])
        last_error = ""
        for attempt in range(BOOTSTRAP_ATTEMPTS):
            result = _run(["launchctl", "bootstrap", f"gui/{uid}", str(path)])
            if result.returncode == 0:
                return
            last_error = result.stderr.strip() or result.stdout.strip()
            # launchd may have raced us into a state where the job is already up,
            # which is success from the caller's point of view.
            if _run(["launchctl", "print", f"gui/{uid}/{SERVICE_LABEL}"]).returncode == 0:
                return
            if attempt < BOOTSTRAP_ATTEMPTS - 1:
                time.sleep(BOOTSTRAP_RETRY_SECONDS)
        raise BridgeServiceError(
            f"launchctl bootstrap failed after {BOOTSTRAP_ATTEMPTS} attempts: {last_error}"
        )
        return
    _run(["systemctl", "--user", "daemon-reload"])
    result = _run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT])
    if result.returncode:
        raise BridgeServiceError(
            f"systemctl enable --now failed: {result.stderr.strip() or result.stdout.strip()}"
        )


def _unload_service() -> None:
    uid = os.getuid()
    if platform.system() == "Darwin":
        _run(["launchctl", "bootout", f"gui/{uid}/{SERVICE_LABEL}"])
        return
    _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])


def install_service(
    port: int = DEFAULT_BRIDGE_PORT,
    credential: str = "claude-code",
    host: str = DEFAULT_BRIDGE_HOST,
    *,
    timeout: float = 20.0,
) -> tuple[Path, str]:
    """Install, start, and health-check the bridge service.

    Returns the unit path and a short human-readable health detail.
    """
    path = write_service_unit(port, credential, host)
    _load_service(path)
    health = wait_for_health(port, host=host, timeout=timeout)
    if health.status == "unreachable":
        raise BridgeServiceError(
            f"the service was installed at {path} but http://{host}:{port}/healthz "
            f"did not answer within {timeout:g}s ({health.detail}); "
            f"check {agentroute_home() / 'bridge.log'}"
        )
    return path, health.detail


def uninstall_service(*, remove_unit: bool = True) -> tuple[Path, bool]:
    """Stop the bridge service and optionally delete its definition."""
    path = service_unit_path()
    existed = path.exists()
    _unload_service()
    if remove_unit and existed:
        path.unlink()
    return path, existed


@dataclass(frozen=True)
class BridgeHealth:
    status: str  # healthy | unhealthy | unreachable | stopped
    detail: str
    payload: dict | None = None


def probe_health(port: int = DEFAULT_BRIDGE_PORT, host: str = DEFAULT_BRIDGE_HOST) -> BridgeHealth:
    """Read the bridge's own health endpoint over loopback."""
    url = f"http://{host}:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as response:
            body = response.read().decode(errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = None
        return BridgeHealth("healthy", "responds on loopback", payload)
    except urllib.error.HTTPError as error:
        return BridgeHealth("unhealthy", f"HTTP {error.code} from {url}", None)
    except urllib.error.URLError as error:
        return BridgeHealth("unreachable", f"no answer from {url} ({error.reason})", None)
    except OSError as error:
        return BridgeHealth("unreachable", f"no answer from {url} ({error})", None)


def wait_for_health(
    port: int = DEFAULT_BRIDGE_PORT,
    host: str = DEFAULT_BRIDGE_HOST,
    *,
    timeout: float = 20.0,
) -> BridgeHealth:
    """Poll the health endpoint until the bridge answers or the timeout expires."""
    deadline = time.monotonic() + timeout
    health = probe_health(port, host)
    while health.status != "healthy" and time.monotonic() < deadline:
        time.sleep(0.25)
        health = probe_health(port, host)
    return health


def service_installed() -> bool:
    return service_unit_path().exists()


def service_loaded() -> bool:
    """Whether the supervisor currently knows about the service."""
    uid = os.getuid()
    if platform.system() == "Darwin":
        return _run(["launchctl", "print", f"gui/{uid}/{SERVICE_LABEL}"]).returncode == 0
    return _run(["systemctl", "--user", "is-active", SYSTEMD_UNIT]).returncode == 0


def service_port() -> int | None:
    """Read the port the installed unit actually uses, if it can be parsed."""
    path = service_unit_path()
    if not path.exists():
        return None
    if platform.system() == "Darwin":
        try:
            with path.open("rb") as handle:
                arguments = plistlib.load(handle).get("ProgramArguments", [])
        except (OSError, plistlib.InvalidFileException):
            return None
    else:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        match = next(
            (line for line in text.splitlines() if line.startswith("ExecStart=")), ""
        )
        arguments = match.removeprefix("ExecStart=").split()
    for index, value in enumerate(arguments):
        if value == "--port" and index + 1 < len(arguments):
            try:
                return int(arguments[index + 1])
            except ValueError:
                return None
    return None
