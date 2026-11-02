"""Grok bridge lifecycle, independent of the Claude service."""

from __future__ import annotations

import hmac
import json
import os
import platform
import plistlib
import secrets
import shlex
import shutil
import subprocess
import tempfile
import urllib.request
from enum import Enum
from pathlib import Path

import typer

from .bridge_service import _agentroute_executable, wait_for_health
from .config import agentroute_home, codex_home, config_path, load_config, save_config
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
from .providers import (
    _launch_model_catalog_path,
    credentials_path,
    import_backend_credential,
    sync_codex_providers,
)

app = typer.Typer(no_args_is_help=True, help="Route through Grok CLI or the public xAI API.")
LABEL = "com.agentroute.grok-bridge"
UNIT = "agentroute-grok-bridge.service"


def unit_path():
    if platform.system() == "Darwin":
        return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    return Path.home() / ".config/systemd/user" / UNIT


class CredentialMode(str, Enum):
    subscription = "subscription"
    api_key = "api-key"


def service_definition(binary, port, credential_mode="subscription", instance=""):
    args = [
        str(_agentroute_executable()),
        "grok",
        "serve",
        "--port",
        str(port),
    ]
    if credential_mode == "api-key":
        args.extend(["--credential", "api-key"])
        if instance:
            args.extend(["--instance", instance])
        environment_names = (
            "AGENTROUTE_HOME", "AGENTROUTE_CREDENTIALS_FILE", "AGENTROUTE_GROK_API_CREDENTIALS_FILE"
        )
    else:
        args.extend(["--grok-binary", binary])
        environment_names = (
            "GROK_HOME", "GROK_AUTH_PATH", "AGENTROUTE_HOME", "AGENTROUTE_CREDENTIALS_FILE"
        )
    environment = {
        key: os.path.abspath(os.path.expanduser(os.environ[key]))
        for key in environment_names
        if key in os.environ and (
            os.environ[key]
            or key not in {"AGENTROUTE_CREDENTIALS_FILE", "AGENTROUTE_GROK_API_CREDENTIALS_FILE"}
        )
    }
    if credential_mode == "api-key":
        # Persist the derived default too, so later subscription setup can retain this boundary.
        environment["AGENTROUTE_CREDENTIALS_FILE"] = os.path.abspath(
            os.path.expanduser(str(credentials_path()))
        )
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
        "[Unit]\nDescription=AgentRoute Grok bridge\nAfter=network-online.target\n"
        f"[Service]\n{environment_lines}ExecStart={command}\nRestart=always\nRestartSec=3\n"
        "[Install]\nWantedBy=default.target\n"
    ).encode()


def run(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True)


@app.command()
def serve(
    port: int = DEFAULT_PORT,
    grok_binary: str = "grok",
    credential: CredentialMode = CredentialMode.subscription,
    instance: str = "",
):
    """Serve the selected credential mode on loopback."""
    source = (
        GrokApiCredential()
        if credential == CredentialMode.api_key
        else GrokCredential(grok_binary)
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


def stop_replacement_service(path, definition):
    import time

    if platform.system() == "Darwin":
        stopped = run("launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}", check=False)
        if stopped.returncode:
            status = run("launchctl", "print", f"gui/{os.getuid()}/{LABEL}", check=False)
            if status.returncode != 113:
                raise RuntimeError(
                    "Grok supervisor rejected stop; its unit is retained for recovery"
                )
        for _attempt in range(10):
            if run("launchctl", "print", f"gui/{os.getuid()}/{LABEL}", check=False).returncode:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Grok service did not stop; its unit is retained for recovery")
    else:
        stopped = run("systemctl", "--user", "stop", UNIT, check=False)
        if stopped.returncode:
            raise RuntimeError("Grok supervisor rejected stop; its unit is retained for recovery")
        if run("systemctl", "--user", "is-active", "--quiet", UNIT, check=False).returncode == 0:
            raise RuntimeError("Grok service did not stop; its unit is retained for recovery")
    if path.exists() and path.read_bytes() == definition:
        path.unlink()


def rollback_install(path, definition, previous_unit, files, expected=None):
    for target, content in files.items():
        current = target.read_bytes() if target.exists() else None
        if current == content:
            continue
        if current != (expected or {}).get(target, content):
            continue  # Preserve edits made by another process; do not restore a stale preimage.
        if content is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(content)
    # Restore client configuration even if stopping the failed service is rejected.
    stop_replacement_service(path, definition)
    if previous_unit is not None:
        path.write_bytes(previous_unit)
        if platform.system() == "Darwin":
            run("launchctl", "bootstrap", f"gui/{os.getuid()}", str(path))
        else:
            run("systemctl", "--user", "daemon-reload")
            run("systemctl", "--user", "restart", UNIT)


def prepare_publication(config, originals):
    """Validate and render through the existing writers before touching live files."""
    native_config = codex_home() / "config.toml"
    native_catalog = _launch_model_catalog_path(native_config)
    native_config.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".grok-config-", dir=native_config.parent) as directory:
        stage = Path(directory)
        staged_config = stage / native_config.name
        staged_config.write_bytes(originals.get(native_config) or b"")
        staged_catalog = _launch_model_catalog_path(staged_config)
        if originals.get(native_catalog) is not None:
            staged_catalog.write_bytes(originals[native_catalog])
        cache = native_config.parent / "models_cache.json"
        if cache.exists():
            (stage / cache.name).write_bytes(cache.read_bytes())
        staged_route = stage / "agentroute.yaml"
        save_config(config, staged_route)
        sync_codex_providers(config, path=staged_config, backup=False)
        codex_payload = staged_config.read_text().replace(
            json.dumps(str(staged_catalog), ensure_ascii=False),
            json.dumps(str(native_catalog), ensure_ascii=False),
        )
        payloads = {native_config: codex_payload.encode()}
        if staged_catalog.exists():
            payloads[native_catalog] = staged_catalog.read_bytes()
        # Enable routing only after provider data.
        payloads[config_path()] = staged_route.read_bytes()
        return payloads


def publish_files(payloads, originals, written):
    for target, payload in payloads.items():
        if target.is_symlink():
            raise ValueError("Refusing to replace a symlinked Grok configuration target")
        current = target.read_bytes() if target.exists() else None
        if current != originals.get(target):
            raise RuntimeError(
                "Configuration changed during Grok install; retry from current state"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
            staged = Path(temporary.name)
            try:
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
                os.replace(staged, target)
                written[target] = payload
            finally:
                staged.unlink(missing_ok=True)


@app.command()
def install(
    port: int = DEFAULT_PORT, credential: CredentialMode = CredentialMode.subscription
):
    """Register @grok, install a user service, and check health."""
    if not 1 <= port <= 65535:
        raise typer.BadParameter("port must be between 1 and 65535")
    path = unit_path()
    if path.exists():
        if platform.system() == "Darwin":
            previous_args = plistlib.loads(path.read_bytes()).get("ProgramArguments", [])
        else:
            previous_args = shlex.split(next(
                row.removeprefix("ExecStart=") for row in path.read_text().splitlines()
                if row.startswith("ExecStart=")
            ))
        if credential == CredentialMode.api_key or "api-key" in previous_args:
            raise typer.BadParameter(
                "Grok service already installed; use status/restart, or uninstall to change modes"
            )
    binary = "grok"
    if credential == CredentialMode.api_key:
        try:
            GrokApiCredential().token()
        except (OSError, ValueError):
            raise typer.BadParameter(
                "Import XAI_API_KEY with agentroute grok credential-import SOURCE_ENV first"
            ) from None
    else:
        binary = shutil.which("grok")
        if not binary:
            raise typer.BadParameter("Install Grok CLI and run grok login first")
        GrokCredential(binary).token()
    config = load_config()
    ensure_backend(config, port, credential.value)
    if credential == CredentialMode.api_key:
        ensure_client_credential(config)
    previous_unit = path.read_bytes() if path.exists() else None
    codex_config = codex_home() / "config.toml"
    publication_files = {
        target: target.read_bytes() if target.exists() else None
        for target in (config_path(), codex_config, _launch_model_catalog_path(codex_config))
    }
    if any(target.is_symlink() for target in publication_files):
        raise typer.BadParameter("Grok installation requires regular configuration files")
    payloads = prepare_publication(config, publication_files)
    if credential == CredentialMode.subscription and path.exists():
        # Preserve the preceding API client's credential path when the new shell omits it.
        if platform.system() == "Darwin":
            old_environment = plistlib.loads(path.read_bytes()).get("EnvironmentVariables", {})
            previous_path = old_environment.get("AGENTROUTE_CREDENTIALS_FILE")
            if not previous_path and old_environment.get("AGENTROUTE_HOME"):
                previous_path = str(
                    Path(old_environment["AGENTROUTE_HOME"]) / "backend-credentials.env"
                )
        else:
            previous_path = None
            for line in path.read_text().splitlines():
                if line.startswith("Environment="):
                    assignment = shlex.split(line.removeprefix("Environment="))[0]
                    name, _, value = assignment.partition("=")
                    if name in {"AGENTROUTE_CREDENTIALS_FILE", "AGENTROUTE_HOME"}:
                        previous_path = value.replace("%%", "%")
                        if name == "AGENTROUTE_HOME":
                            previous_path = str(Path(previous_path) / "backend-credentials.env")
        if previous_path and not os.environ.get("AGENTROUTE_CREDENTIALS_FILE"):
            os.environ["AGENTROUTE_CREDENTIALS_FILE"] = previous_path
    path.parent.mkdir(parents=True, exist_ok=True)
    instance = secrets.token_hex(32) if credential == CredentialMode.api_key else ""
    definition = service_definition(binary, port, credential.value, instance)
    path.write_bytes(definition)
    try:
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
        if (
            health.status != "healthy"
            or (health.payload or {}).get("provider") != "grok"
            or (health.payload or {}).get("credential_mode", "subscription") != credential.value
        ):
            raise RuntimeError(f"Grok service did not become healthy: {health.detail}")
        if credential == CredentialMode.api_key:
            nonce = secrets.token_hex(32)
            expected = GrokApiCredential().health_proof(nonce, instance, port)
            request = urllib.request.Request(f"http://127.0.0.1:{port}/healthz?nonce={nonce}")
            from .grok_bridge import open_upstream

            with open_upstream(request, timeout=5) as response:
                payload = json.loads(response.read(65536))
            actual = payload.get("credential_proof", "") if isinstance(payload, dict) else ""
            if not isinstance(actual, str) or not hmac.compare_digest(actual, expected):
                raise RuntimeError(
                    "Grok service credential proof failed; check the configured port"
                )
    except BaseException:
        rollback_install(path, definition, previous_unit, {})
        raise
    from .grok_usage import read_snapshot, write_snapshot

    source = GrokApiCredential() if credential == CredentialMode.api_key else GrokCredential(binary)
    try:
        previous = read_snapshot(source)
        write_snapshot(
            source,
            {"blocked_until": previous["blocked_until"]} if "blocked_until" in previous else {},
            account=previous.get("account"),
        )
    except (OSError, ValueError):
        # Optional capacity persistence cannot turn a proven ready service into a failed install.
        typer.echo("Grok is ready; capacity snapshot unavailable", err=True)
    # A failed service/account check must not publish an enabled client route.
    published = {}
    try:
        publish_files(payloads, publication_files, published)
    except BaseException:
        # Any publication failure must restore custody, including malformed cached metadata.
        rollback_install(path, definition, previous_unit, publication_files, published)
        raise
    typer.echo(f"Grok {credential.value} ready on port {port}. Use @grok in Codex.")


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
