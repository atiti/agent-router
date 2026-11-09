"""Herdr terminal views over the shared Codex owner; chats stay in app-server."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import UUID

import typer

from . import zellij_cli as chats
from .config import agentroute_home, load_config, save_config
from .server_cli import _start
from .shared_server import RpcClient, SharedServerError, socket_path, state_dir

herdr_app = typer.Typer(no_args_is_help=True, help="Manage shared chats in Herdr TUI.")
herdr_app.command("list")(chats.list_command)
herdr_app.add_typer(chats.group_app, name="group")
SESSION = "agentroute"
SOURCE = "agentroute"


def command(session: str, *args: str) -> dict:
    result = subprocess.run([chats.executable("herdr"), "--session", session, *args],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise SharedServerError(result.stderr.strip() or result.stdout.strip())
    if not result.stdout.strip():
        return {}  # Herdr mutation commands may acknowledge with no output.
    value = json.loads(result.stdout)
    if "error" in value:
        raise SharedServerError(str(value["error"]))
    return value["result"]


def panes(session: str) -> list[dict]:
    return command(session, "pane", "list")["panes"]


def registry_path(session: str, thread: str) -> Path:
    import hashlib
    key = hashlib.sha256(session.encode()).hexdigest()[:16]
    return state_dir() / "herdr-targets" / key / f"{UUID(thread)}.json"


def thread_panes(items: list[dict], session: str = SESSION) -> dict[str, dict]:
    directory = registry_path(session, "00000000-0000-0000-0000-000000000000").parent
    by_terminal = {p["terminal_id"]: p for p in items}
    found = {}
    for path in directory.glob("*.json"):
        try:
            record = json.loads(path.read_text())
            os.kill(record["pid"], 0)
            pane = by_terminal.get(record["terminal_id"])
            if pane:
                found[str(UUID(path.stem))] = pane
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return found


def focus_pane(session: str, pane: dict) -> None:
    # tab focus selects its existing active pane; agent focus selects the exact pane.
    command(session, "workspace", "focus", pane["workspace_id"])
    command(session, "tab", "focus", pane["tab_id"])
    command(session, "agent", "focus", pane["pane_id"])


def open_thread(thread: dict, session: str) -> str:
    with chats.operation_lock():
        existing = thread_panes(panes(session), session).get(thread["id"])
        if existing:
            focus_pane(session, existing)
            return existing["pane_id"]
        label = chats.thread_namespace(thread)
        workspaces = command(session, "workspace", "list")["workspaces"]
        workspace = next((w for w in workspaces if w.get("label") == label), None)
        args = ["tab", "create", "--workspace", workspace["workspace_id"]] if workspace else [
            "workspace", "create"]
        created = command(session, *args, "--cwd", thread["cwd"], "--label",
                          chats.title(thread) if workspace else label, "--focus")
        pane = created["root_pane"]
        argv = [sys.executable, "-m", "agentroute.cli", "herdr", "client",
                "--thread", thread["id"], "--session", session]
        command(session, "pane", "run", pane["pane_id"], shlex.join(argv))
        # The client reports its identity before launching Codex. Wait under the lock
        # so a second opener cannot race that registration and spawn another writer.
        for _ in range(50):
            if thread["id"] in thread_panes(panes(session), session):
                return pane["pane_id"]
            time.sleep(.1)
        raise SharedServerError(
            "Herdr client did not register; inspect the new tab before retrying.")


@herdr_app.command("open")
def open_command(query: str, session: str = SESSION,
                 namespaces: list[str] | None = typer.Option(None, "--namespace", "-n")) -> None:
    """Open/resume a shared chat, or focus its existing Herdr view."""
    try:
        with RpcClient(socket_path()) as client:
            thread = chats.resolve(client, query, namespaces)
        typer.echo(open_thread(thread, session))
    except chats.ERRORS as error:
        raise typer.BadParameter(str(error)) from error


@herdr_app.command("focus")
def focus_command(query: str, session: str = SESSION,
                  namespaces: list[str] | None = typer.Option(None, "--namespace", "-n")) -> None:
    """Focus an existing view without launching or resuming a chat."""
    try:
        with RpcClient(socket_path()) as client:
            thread = chats.resolve(client, query, namespaces)
        pane = thread_panes(panes(session), session).get(thread["id"])
        if not pane:
            raise SharedServerError("No Herdr view; use ar open <chat> first.")
        focus_pane(session, pane)
        typer.echo(pane["pane_id"])
    except chats.ERRORS as error:
        raise typer.BadParameter(str(error)) from error


@herdr_app.command("new")
def new_command(directory: Path = typer.Option(Path.cwd(), "--dir", exists=True, file_okay=False),
                name: str = "", session: str = SESSION) -> None:
    """Create a shared chat and open it in its namespace workspace."""
    try:
        panes(session)
        with RpcClient(socket_path(), timeout=30) as client:
            thread = client.request("thread/start", {"cwd": str(directory.resolve())})["thread"]
            if name:
                client.request("thread/name/set", {"threadId": thread["id"], "name": name})
                thread["name"] = name
        typer.echo(open_thread(thread, session))
    except chats.ERRORS as error:
        raise typer.BadParameter(str(error)) from error


def report(session: str, pane: str, thread: str, state: str) -> None:
    command(session, "pane", "report-agent", pane, "--source", SOURCE, "--agent", "codex",
            "--state", state, "--agent-session-id", thread)


@herdr_app.command("client", hidden=True)
def client_command(thread: str = typer.Option(...), session: str = SESSION) -> None:
    thread = str(UUID(thread))
    if not load_config().shared_server.enabled:
        raise typer.BadParameter("Shared attachment is disabled; run agentroute server enable.")
    pane = os.environ["HERDR_PANE_ID"]
    identity = command(session, "pane", "get", pane)["pane"]
    target = registry_path(session, thread)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text(json.dumps({"pid": os.getpid(), "terminal_id": identity["terminal_id"]}))
    target.chmod(0o600)
    report(session, pane, thread, "unknown")
    # Resume through AgentRoute so routing and shared-owner writer guards survive restarts.
    command(session, "pane", "report-agent-session", pane, "--source", SOURCE,
            "--agent", "codex", "--agent-session-id", thread, "--",
            "agentroute", "herdr", "client", "--thread", thread, "--session", session)
    stopped = threading.Event()

    def monitor() -> None:
        while not stopped.is_set():
            try:
                with RpcClient(socket_path(), timeout=5) as client:
                    current = client.request("thread/read", {"threadId": thread})["thread"]
                state = {"RUNNING": "working", "APPROVAL": "blocked", "INPUT": "blocked",
                         "READY": "idle"}.get(chats.status(current), "unknown")
                report(session, pane, thread, state)
            except chats.ERRORS:
                pass  # A status outage must not terminate the terminal client.
            stopped.wait(2)

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    try:
        result = subprocess.run([sys.executable, "-m", "agentroute.cli", "launch-codex",
                                 "--", "resume", thread], check=False,
                                env={**os.environ, "HERDR_AGENT": "codex"})
    finally:
        target.unlink(missing_ok=True)
        stopped.set()
        watcher.join(timeout=12)
        try:
            command(session, "pane", "release-agent", pane, "--source", SOURCE, "--agent", "codex")
        except chats.ERRORS:
            pass
    raise typer.Exit(result.returncode)


@herdr_app.command("start")
def start_command(session: str = SESSION, background: bool = False) -> None:
    """Start/reuse the shared owner and dedicated persistent Herdr server."""
    try:
        binary = chats.executable("herdr")
        _start()
        config = load_config()
        if not config.shared_server.enabled:
            config.shared_server.enabled = True
            save_config(config)
        try:
            panes(session)
        except chats.ERRORS:
            state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
            with (state_dir() / "herdr-server.log").open("ab") as log:
                process = subprocess.Popen([binary, "--session", session, "server"],
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                           start_new_session=True)
            for _ in range(50):
                try:
                    panes(session)
                    break
                except chats.ERRORS:
                    if process.poll() is not None:
                        raise SharedServerError(
                            "Herdr server exited; inspect herdr-server.log") from None
                    time.sleep(.1)
            else:
                raise SharedServerError("Herdr server not ready; inspect herdr-server.log")
        if not background:
            os.execvp(binary, [binary, "--session", session])
        typer.echo(f"Workspace ready. Attach with: herdr --session {shlex.quote(session)}")
    except chats.ERRORS as error:
        raise typer.BadParameter(str(error)) from error


@herdr_app.command("install")
def install_command(bin_dir: Path | None = typer.Option(None)) -> None:
    """Select Herdr for ar/focus. Existing Zellij sessions remain running."""
    directory = bin_dir or agentroute_home() / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    launcher = shlex.quote(str(agentroute_home() / "bin" / "agentroute"))
    scripts = {"ar": 'if [ "$#" -eq 0 ]; then set -- start; fi\n'
               + f'exec {launcher} herdr "$@"\n',
               "focus": f'exec {launcher} herdr focus "$@"\n'}
    for name in scripts:
        path = directory / name
        if path.exists() and not any(marker in path.read_text() for marker in (
                "# AgentRoute Zellij cockpit", "# AgentRoute Herdr cockpit")):
            raise typer.BadParameter(f"Refusing to replace an unrelated command: {path}")
    for name, body in scripts.items():
        path = directory / name
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(f"#!/bin/sh\n# AgentRoute Herdr cockpit\nset -eu\n{body}")
        temporary.chmod(0o755)
        temporary.replace(path)
    typer.echo(f"Installed Herdr ar/focus commands in {directory}")
