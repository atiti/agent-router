"""A private, persistent patched Codex owner shared by terminal and mobile clients.

Keep this separate from Codex's managed package updater: that updater can replace
the routed executable with an upstream build. The OS thread writer lock stays intact.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from websockets.exceptions import WebSocketException
from websockets.sync.client import unix_connect

from .config import AppConfig, agentroute_home, codex_home


class SharedServerError(RuntimeError):
    pass


def state_dir() -> Path:
    # Different CODEX_HOME values must never attach to each other's accounts/history.
    identity = hashlib.sha256(str(codex_home().resolve()).encode()).hexdigest()[:12]
    return agentroute_home() / "shared-server" / identity


def socket_path() -> Path:
    path = state_dir() / "server.sock"
    if len(os.fsencode(path)) >= 104:
        raise SharedServerError("shared server socket path is too long; shorten AGENTROUTE_HOME")
    return path


class RpcClient:
    """Small synchronous client for the app-server's WebSocket-over-Unix protocol."""

    def __init__(self, path: Path, name: str = "agentroute_shared_server", timeout: float = 5):
        self.timeout = timeout
        self.sequence = 0
        self.events: list[dict[str, Any]] = []
        self.connection = unix_connect(
            str(path), open_timeout=timeout, close_timeout=1, max_size=64 * 1024 * 1024
        )
        try:
            self.info = self.request(
                "initialize",
                {
                    "clientInfo": {"name": name, "title": "AgentRoute", "version": "0.1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.connection.send(json.dumps({"method": "initialized"}))
        except Exception:
            self.close()
            raise

    def request(self, method: str, params: Any = None) -> Any:
        self.sequence += 1
        request_id = self.sequence
        self.connection.send(json.dumps({"id": request_id, "method": method, "params": params}))
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SharedServerError(f"timed out waiting for {method}")
            message = json.loads(self.connection.recv(timeout=remaining))
            if message.get("id") == request_id and ("result" in message or "error" in message):
                if "error" in message:
                    raise SharedServerError(f"{method}: {message['error']['message']}")
                return message["result"]
            self.events.append(message)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> RpcClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def server_argv(binary: Path, config: AppConfig) -> list[str]:
    from .launcher import BASE_CODEX_ARGS, startup_route_args

    route = startup_route_args(config, [])
    # --model is a TUI setting; app-server loads its default from config overrides.
    model_index = route.index("--model")
    route[model_index : model_index + 2] = ["-c", f"model={json.dumps(route[model_index + 1])}"]
    return [
        str(binary.resolve()),
        *BASE_CODEX_ARGS,
        *route,
        *(arg for setting in config.shared_server.startup_config for arg in ("-c", setting)),
        "app-server",
        "--listen",
        f"unix://{socket_path()}",
        "--remote-control",
        *(
            ["--analytics-default-enabled"]
            if config.shared_server.analytics_default_enabled
            else []
        ),
    ]


def _fingerprint(binary: Path, config: AppConfig, environment: Mapping[str, str]) -> str:
    stat = binary.stat()
    # No credentials are serialized. Changes require an explicit, safe restart.
    settings = [
        server_argv(binary, config),
        stat.st_mtime_ns,
        stat.st_size,
        environment.get("AGENTROUTE_RUNTIME_BUILD_ID"),
        environment.get("CODEX_ROLLOUT_TRACE_ROOT"),
        {
            backend.api_key_env: environment.get(backend.api_key_env)
            for backend in config.backends.values()
            if backend.enabled and backend.api_key_env
        },
    ]
    return hashlib.sha256(json.dumps(settings).encode()).hexdigest()


def server_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    from .context_profile import capture_enabled, profile_root
    from .providers import credentials_path

    result = dict(os.environ if environment is None else environment)
    credentials = credentials_path()
    if credentials.exists():
        # Parse the export NAME=<shlex-quoted literal> format produced by AgentRoute.
        # Do not execute shell code or expand substitutions from the credential file.
        for line in credentials.read_text().splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            words = shlex.split(line)
            if len(words) != 2 or words[0] != "export" or "=" not in words[1]:
                raise SharedServerError("invalid AgentRoute credential export format")
            name, value = words[1].split("=", 1)
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
                raise SharedServerError("invalid AgentRoute credential variable name")
            # Match the codex shell wrapper, which sources these stored exports
            # after inheriting its environment. Desktop can retain older values.
            result[name] = value
    receipt = agentroute_home() / "build-id"
    if receipt.exists():
        result.setdefault("AGENTROUTE_RUNTIME_BUILD_ID", receipt.read_text().strip())
    if capture_enabled() and not result.get("CODEX_ROLLOUT_TRACE_ROOT"):
        root = profile_root() / "traces"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        result["CODEX_ROLLOUT_TRACE_ROOT"] = str(root)
    return result


@contextmanager
def _operation_lock() -> Iterator[None]:
    if os.name != "posix":
        raise SharedServerError("shared terminal/mobile sessions currently require macOS or Linux")
    import fcntl

    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    with (directory / "operation.lock").open("a") as handle:
        os.chmod(handle.name, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _metadata() -> dict[str, Any]:
    try:
        return json.loads((state_dir() / "owner.json").read_text())
    except (OSError, ValueError):
        return {}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _owns_process(metadata: dict[str, Any]) -> bool:
    pid = metadata.get("pid")
    if not isinstance(pid, int) or pid <= 1 or not _alive(pid):
        return False
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False
    )
    return (
        str(socket_path()) in result.stdout
        and "app-server" in result.stdout
        and metadata.get("binary", "\0") in result.stdout
    )


def server_status() -> dict[str, Any]:
    metadata = _metadata()
    try:
        with RpcClient(socket_path(), timeout=2) as client:
            remote = client.request("remoteControl/status/read")
            status = {
                "running": True,
                "socket": str(socket_path()),
                "pid": metadata.get("pid"),
                "remoteControl": remote,
            }
            if remote.get("status") == "errored":
                log_path = state_dir() / "server.log"
                if log_path.exists():
                    with log_path.open("rb") as log:
                        log.seek(max(0, log_path.stat().st_size - 16384))
                        recent = log.read().decode("utf-8", errors="replace")
                    if "Remote app server already online" in recent:
                        status["mobileHint"] = (
                            "Another app-server owns this mobile host registration. "
                            "Turn off remote control in the running Desktop, or close and "
                            "reopen routed Desktop so it joins the shared owner."
                        )
            return status
    except (OSError, TimeoutError, SharedServerError, WebSocketException) as error:
        return {"running": False, "socket": str(socket_path()), "error": str(error)}


def ensure_server(
    binary: Path,
    config: AppConfig,
    environment: Mapping[str, str] | None = None,
    timeout: float = 15,
) -> Path:
    environment = server_environment(environment)
    fingerprint = _fingerprint(binary, config, environment)
    with _operation_lock():
        metadata = _metadata()
        status = server_status()
        if status["running"]:
            if metadata.get("fingerprint") != fingerprint or not _owns_process(metadata):
                raise SharedServerError(
                    "shared server runtime/settings differ from this launch; finish its sessions, "
                    "then run `agentroute server stop` and start again"
                )
            return socket_path()
        if _owns_process(metadata):
            raise SharedServerError(
                "shared server process is running but unresponsive; inspect "
                f"{state_dir() / 'server.log'} before stopping it"
            )
        socket_path().unlink(missing_ok=True)
        log_path = state_dir() / "server.log"
        with log_path.open("ab") as log:
            log_path.chmod(0o600)
            child = subprocess.Popen(
                server_argv(binary, config),
                env=environment,
                cwd=codex_home(),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        metadata = {"pid": child.pid, "binary": str(binary.resolve()), "fingerprint": fingerprint}
        metadata_path = state_dir() / "owner.json"
        metadata_path.write_text(json.dumps(metadata))
        metadata_path.chmod(0o600)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise SharedServerError(f"shared server exited; inspect {log_path}")
            if server_status()["running"]:
                socket_path().chmod(0o600)
                return socket_path()
            time.sleep(0.1)
        # Preserve the process and its metadata for diagnosis; never spawn a second owner.
        raise SharedServerError(f"shared server did not become ready; inspect {log_path}")


def stop_server(force: bool = False) -> bool:
    with _operation_lock():
        metadata = _metadata()
        if not _owns_process(metadata):
            if server_status()["running"]:
                raise SharedServerError("refusing to stop a server without matching owner metadata")
            return False
        if not force:
            with RpcClient(socket_path()) as client:
                loaded = client.request("thread/loaded/list", {"limit": 1})
                if loaded.get("data"):
                    raise SharedServerError(
                        "shared server has loaded sessions; stopping can interrupt work. "
                        "Use `agentroute server stop --force` only when ready"
                    )
        os.kill(metadata["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 10
        while _owns_process(metadata) and time.monotonic() < deadline:
            time.sleep(0.1)
        if _owns_process(metadata):
            raise SharedServerError("server has not stopped yet; wait before retrying")
        (state_dir() / "owner.json").unlink(missing_ok=True)
        socket_path().unlink(missing_ok=True)
        return True


def pair_server() -> dict[str, Any]:
    with RpcClient(socket_path(), timeout=15) as client:
        return client.request("remoteControl/pairing/start", {"manualCode": True})


def proxy_stdio(endpoint: Path) -> None:
    """Forward Desktop's JSON lines unchanged to the owner's Unix WebSocket.

    Codex's `app-server proxy` relays raw socket bytes, while this control listener
    requires a WebSocket handshake. Initialization and server requests remain the
    Desktop client's responsibility; this adapter owns no agent state.
    """
    with unix_connect(
        str(endpoint), open_timeout=10, close_timeout=1, max_size=64 * 1024 * 1024
    ) as connection:
        errors: list[Exception] = []

        def input_loop() -> None:
            try:
                for line in sys.stdin:
                    if line.strip():
                        connection.send(line.rstrip("\n"))
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()

        threading.Thread(target=input_loop, daemon=True).start()
        for message in connection:
            if isinstance(message, bytes):
                message = message.decode("utf-8")
            sys.stdout.write(message + "\n")
            sys.stdout.flush()
        if errors:
            raise SharedServerError(f"Desktop stdio proxy failed: {errors[0]}")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "proxy":
        raise SystemExit("usage: python -m agentroute.shared_server proxy SOCKET_PATH")
    proxy_stdio(Path(sys.argv[2]))
