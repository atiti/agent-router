"""Herdr terminal views over the shared Codex owner; chats stay in app-server."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import typer

from . import zellij_cli as chats
from .config import agentroute_home, load_config, save_config
from .server_cli import _start
from .shared_server import (
    RpcClient,
    SharedServerError,
    ensure_server,
    socket_path,
    state_dir,
)

herdr_app = typer.Typer(no_args_is_help=True, help="Manage shared chats in Herdr TUI.")
herdr_app.command("list")(chats.list_command)
herdr_app.add_typer(chats.group_app, name="group")
SESSION = "agentroute"
# Herdr stores a pane's chat identity only from its own Codex integration. That hook runs in
# the shared owner, which serves every pane and so cannot name one; the launcher in the pane
# sends the same report instead.
SESSION_SOURCE = "herdr:codex"
# Codex cuts the thread part of its terminal title to this many characters.
TITLE_THREAD_CHARS = 48
FOLLOW_SECONDS = 3.0
RECHECK_SECONDS = 60.0


def command(session: str | None, *args: str) -> dict:
    result = subprocess.run([chats.executable("herdr"),
                             *(["--session", session] if session else []), *args],
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


def session_key(session: str) -> str:
    return hashlib.sha256(session.encode()).hexdigest()[:16]


def layout_directory(session: str) -> Path:
    return state_dir() / "herdr-layout" / session_key(session)


def remember_pane(session: str, pane: dict, thread: str) -> Path:
    directory = layout_directory(session)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(pane["pane_id"].encode()).hexdigest()[:16]
    path = directory / f"{key}.json"
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    record = {name: pane[name] for name in (
        "pane_id", "workspace_id", "tab_id", "terminal_id", "cwd",
    )}
    record["thread"] = str(UUID(thread))
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(record, f)
    temporary.replace(path)
    return path


def saved_panes(session: str) -> Iterator[dict[str, str]]:
    for path in sorted(layout_directory(session).glob("*.json")):
        try:
            record = json.loads(path.read_text())
            if not all(isinstance(record.get(key), str) for key in (
                "pane_id", "workspace_id", "tab_id", "terminal_id", "cwd",
            )):
                continue
            record["thread"] = str(UUID(record["thread"]))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        yield record


def native_thread(pane: dict) -> str | None:
    ref = pane.get("agent_session") or {}
    if ref.get("source") == "herdr:codex" and ref.get("kind") == "id":
        try:
            return str(UUID(ref["value"]))
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
    return None


def thread_label(thread: dict) -> str | None:
    """A named thread as Codex writes it in the terminal title; None for an unnamed one."""
    name = str(thread.get("name") or "").strip()
    if not name:
        return None
    if len(name) <= TITLE_THREAD_CHARS:
        return name
    # Codex counts grapheme clusters. A name with combined characters near the cut will not
    # compare equal here, which leaves that view unidentified rather than misidentified.
    return name[:TITLE_THREAD_CHARS - 3] + "..."


def title_thread(title: str) -> str | None:
    """The thread part of a Codex terminal title in its default layout, `name | project`.

    An unnamed chat shows only the project, so a title in one part names no thread.
    """
    label, separator, _project = title.rpartition(" | ")
    return label if separator and label else None


def shown_thread(client: RpcClient, pane: dict, known: str | None) -> tuple[str | None, bool]:
    """The chat a pane's Codex view shows, and whether the terminal title proves it.

    A view can move to another chat (/new, /resume, a fork) without telling anyone. The title
    is the only thing it publishes about itself, and it carries a name, not an identifier. So
    the answer is certain only when exactly one thread the shared owner has loaded bears the
    name in the title. In every other case the earlier identity is returned as unconfirmed,
    or None when the title shows the view has left it for a chat that cannot be told apart.
    """
    shown = title_thread(pane.get("terminal_title_stripped") or "")
    threads = {}
    for thread_id in client.request("thread/loaded/list", {}).get("data") or []:
        try:
            threads[thread_id] = client.request("thread/read", {"threadId": thread_id})["thread"]
        except (SharedServerError, KeyError, TypeError):
            continue
    if shown is None:
        # Nothing drawn yet, a custom layout, or an unnamed chat. A named chat would show
        # its name, so a view that was on one has moved somewhere this cannot follow.
        moved = known in threads and thread_label(threads[known]) is not None \
            and bool(pane.get("terminal_title_stripped"))
        return (None if moved else known), False
    matches = [thread_id for thread_id, thread in threads.items()
               if thread_label(thread) == shown]
    if len(matches) == 1:
        return matches[0], True
    if known in matches:
        return known, False  # Another loaded chat has the same name; nothing separates them.
    return None, False


def report_session(session: str, pane: str, thread: str) -> None:
    """Tell Herdr which chat a pane shows, and how to reopen it after a Herdr restart."""
    command(session, "pane", "report-agent-session", pane, "--source", SESSION_SOURCE,
            "--agent", "codex", "--agent-session-id", thread, "--",
            "agentroute", "herdr", "client", "--thread", thread, "--session", session)


class PaneFollower:
    """Keeps a pane's saved chat identity equal to the chat its Codex view shows."""

    def __init__(self, session: str, pane: str, thread: str | None):
        self.session = session
        self.pane = pane
        self.thread = thread
        self.terminal: str | None = None
        self.binding: tuple[str, str] | None = None
        self.binding_path: Path | None = None
        self._checked: tuple[str, str, str | None] | None = None
        self._checked_at = 0.0

    def pane_info(self) -> dict | None:
        try:
            info = command(self.session, "pane", "get", self.pane).get("pane") or {}
        except SharedServerError:
            info = {}
        if not info or (self.terminal and info.get("terminal_id") != self.terminal):
            # Moving a pane changes its ID; its terminal ID stays the same.
            if not self.terminal:
                return None
            info = next((p for p in panes(self.session)
                         if p.get("terminal_id") == self.terminal), None)
        return info

    def step(self) -> None:
        info = self.pane_info()
        if not info or not info.get("terminal_id"):
            return
        self.terminal, self.pane = info["terminal_id"], info["pane_id"]
        seen = (self.pane, info.get("terminal_title_stripped") or "", native_thread(info))
        if seen == self._checked and time.monotonic() - self._checked_at < RECHECK_SECONDS:
            return
        with RpcClient(socket_path(), timeout=5) as client:
            thread, certain = shown_thread(client, info, self.thread)
        self._checked, self._checked_at = seen, time.monotonic()
        if self.binding is None and self.thread and not certain:
            thread = self.thread  # The launch command is exact when the view starts.
        elif not certain:
            return  # Only an identity the title proves may replace what is recorded.
        self.thread = thread
        if self.binding != (self.pane, thread):
            path = remember_pane(self.session, info, thread)
            if self.binding_path and self.binding_path != path:
                self.binding_path.unlink(missing_ok=True)
            self.binding_path, self.binding = path, (self.pane, thread)
        if native_thread(info) != thread:
            report_session(self.session, self.pane, thread)
            self._checked = (seen[0], seen[1], thread)

    def run(self, stopped: threading.Event) -> None:
        while not stopped.is_set():
            try:
                self.step()
            except Exception:  # noqa: BLE001
                # This thread lives as long as the chat. One bad response from Herdr or the
                # shared owner must not end identity tracking for the rest of the session.
                pass
            stopped.wait(FOLLOW_SECONDS)


def registry_path(session: str, thread: str) -> Path:
    key = session_key(session)
    return state_dir() / "herdr-targets" / key / f"{UUID(thread)}.json"


def thread_panes(items: list[dict], session: str = SESSION) -> dict[str, dict]:
    directory = registry_path(session, "00000000-0000-0000-0000-000000000000").parent
    by_terminal = {p["terminal_id"]: p for p in items}
    found = {}
    current = {p["pane_id"]: p for p in items}
    tracked = {}
    for record in saved_panes(session):
        pane = current.get(record["pane_id"])
        if pane and pane["terminal_id"] == record["terminal_id"] and pane.get("agent") == "codex":
            tracked[pane["terminal_id"]] = record["thread"]
            found[record["thread"]] = pane
    for path in directory.glob("*.json"):
        try:
            record = json.loads(path.read_text())
            os.kill(record["pid"], 0)
            pane = by_terminal.get(record["terminal_id"])
            if pane and pane["terminal_id"] not in tracked:
                found[str(UUID(path.stem))] = pane
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return found


def focus_pane(session: str, pane: dict) -> None:
    # tab focus selects its existing active pane; agent focus selects the exact pane.
    command(session, "workspace", "focus", pane["workspace_id"])
    command(session, "tab", "focus", pane["tab_id"])
    command(session, "agent", "focus", pane["pane_id"])


def launch_client(session: str, pane: str, thread: str) -> None:
    argv = [sys.executable, "-m", "agentroute.cli", "herdr", "client",
            "--thread", thread, "--session", session]
    command(session, "pane", "run", pane, shlex.join(argv))


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
        launch_client(session, pane["pane_id"], thread["id"])
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


def create_thread(directory: Path, endpoint: Path, name: str = "") -> dict:
    # Naming the chat commits Codex's lazy rollout before the TUI attaches by ID.
    name = name or "New chat"
    with RpcClient(endpoint, timeout=30) as client:
        thread = client.request("thread/start", {"cwd": str(directory.resolve())})["thread"]
        client.request("thread/name/set", {"threadId": thread["id"], "name": name})
        thread["name"] = name
    return thread


def track_new_chat(argv: list[str], endpoint: Path) -> list[str]:
    try:
        thread = create_thread(Path.cwd(), endpoint)
        return [*argv, "resume", str(UUID(thread["id"]))]
    except chats.ERRORS + (KeyError,):
        # Startup must work even if the optional status integration is unavailable.
        return argv


@herdr_app.command("new")
def new_command(directory: Path = typer.Option(Path.cwd(), "--dir", exists=True, file_okay=False),
                name: str = "", session: str = SESSION) -> None:
    """Create a shared chat and open it in its namespace workspace."""
    try:
        panes(session)
        thread = create_thread(directory, socket_path(), name)
        typer.echo(open_thread(thread, session))
    except chats.ERRORS as error:
        raise typer.BadParameter(str(error)) from error


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
    try:
        # Resume through AgentRoute so routing and shared-owner writer guards survive restarts.
        report_session(session, pane, thread)
    except chats.ERRORS:
        pass  # The launcher repeats this once the pane is recognised as Codex.
    try:
        result = subprocess.run([sys.executable, "-m", "agentroute.cli", "launch-codex",
                                 "--", "resume", thread], check=False,
                                env={**os.environ, "HERDR_AGENT": "codex"})
    finally:
        target.unlink(missing_ok=True)
    raise typer.Exit(result.returncode)


def start_herdr(session: str) -> tuple[str, bool]:
    binary = chats.executable("herdr")
    started = False
    try:
        panes(session)
    except chats.ERRORS:
        started = True
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
    return binary, started


@herdr_app.command("start")
def start_command(session: str = SESSION, background: bool = False) -> None:
    """Start/reuse the shared owner and dedicated persistent Herdr server."""
    try:
        _start()
        config = load_config()
        if not config.shared_server.enabled:
            config.shared_server.enabled = True
            save_config(config)
        binary, _ = start_herdr(session)
        if not background:
            os.execvp(binary, [binary, "--session", session])
        typer.echo(f"Workspace ready. Attach with: herdr --session {shlex.quote(session)}")
    except chats.ERRORS as error:
        raise typer.BadParameter(str(error)) from error


def wait_for_shell(session: str, pane: str, starting: bool) -> bool:
    for _ in range(25 if starting else 1):
        info = command(session, "pane", "process-info", "--pane", pane)["process_info"]
        processes = info["foreground_processes"]
        if (info["shell_pid"] and info["foreground_process_group_id"] == info["shell_pid"]
                and len(processes) == 1
                and processes[0].get("name") in {"bash", "fish", "sh", "zsh"}):
            return True
        if starting:
            time.sleep(.2)
    return False


def restore_pane(
    session: str, record: dict[str, str], pane: dict | None, used: set[str], starting: bool,
) -> str:
    thread = record["thread"]
    pane_id = record["pane_id"]
    if not pane or any(pane.get(k) != record[k] for k in ("workspace_id", "tab_id", "cwd")):
        return "missing or moved"
    if pane["terminal_id"] == record["terminal_id"] or thread in used:
        return "already present or original terminal still open"
    if not wait_for_shell(session, pane_id, starting):
        return "occupied"
    launch_client(session, pane_id, thread)
    used.add(thread)
    for _ in range(50):
        if thread_panes(panes(session), session).get(thread, {}).get("pane_id") == pane_id:
            return "started"
        time.sleep(.1)
    return "error: launch was sent but the client did not register; inspect the pane"


def restore_panes(session: str, starting: bool = False) -> list[dict[str, str]]:
    results = []
    with chats.operation_lock():
        current = panes(session)
        by_pane = {pane["pane_id"]: pane for pane in current}
        used = set(thread_panes(current, session))
        for record in saved_panes(session):
            thread, pane_id = record["thread"], record["pane_id"]
            try:
                status = restore_pane(session, record, by_pane.get(pane_id), used, starting)
            except chats.ERRORS as error:
                status = f"error: {error}"
            results.append({"pane": pane_id, "thread": thread, "status": status})
    return results


@herdr_app.command("restore")
def restore_command(session: str = SESSION, attach: bool = False) -> None:
    """Reopen saved shared chats in their original panes after a Herdr restart."""
    try:
        if not load_config().shared_server.enabled:
            raise SharedServerError("Shared attachment is disabled; run agentroute server enable.")
        _start()
        binary, started = start_herdr(session)
        from .herdr_team import restore_team_views

        results = restore_panes(session, starting=started)
        results.extend(restore_team_views(session, starting=started))
        for row in results:
            typer.echo(f'{row["pane"]}  {row["thread"]}  {row["status"]}')
        if not results:
            typer.echo("No saved AgentRoute pane bindings for this Herdr session.")
        if any(row["status"].startswith("error: ") for row in results):
            raise typer.Exit(1)
        if attach:
            os.execvp(binary, [binary, "--session", session])
    except chats.ERRORS as error:
        raise typer.BadParameter(str(error)) from error


@herdr_app.command("r")
def restore_shortcut(session: str = SESSION) -> None:
    """Restore saved chats and attach to Herdr."""
    restore_command(session=session, attach=True)


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


def codex_view(session: str, pane: str) -> dict | None:
    """The Codex client in a pane's foreground, only if it is attached to this shared owner.

    A Codex started with --no-daemon or pointed at another server owns its chat: stopping it
    would end that chat, so it is never treated as a view.
    """
    info = command(session, "pane", "process-info", "--pane", pane)["process_info"]
    endpoint = f"unix://{socket_path()}"
    for process in info.get("foreground_processes", []):
        argv = process.get("argv") or []
        if process.get("name") == "codex-bin" and "--remote" in argv[:-1] \
                and argv[argv.index("--remote") + 1] == endpoint:
            return process
    return None


def relaunch_check(session: str, pane_id: str, thread: str | None,
                   force: bool) -> tuple[dict, dict, str] | str:
    """Everything that must hold to restart a view: its pane, process and chat, or a reason."""
    try:
        pane = command(session, "pane", "get", pane_id)["pane"]
    except SharedServerError:
        return "error: pane not found"
    view = codex_view(session, pane_id)
    if not view:
        return "no view of this shared owner"
    # Only the command that started the view, or the operator, can say which chat it is.
    thread = thread or launch_thread(view.get("argv") or [])
    if not thread:
        return "skipped: the view was started without a chat ID; pass --thread to name it"
    with RpcClient(socket_path(), timeout=5) as client:
        shown, certain = shown_thread(client, pane, thread)
        if shown != thread or not certain:
            return "skipped: the title does not single out this chat"
        state = chats.status(client.request("thread/read", {"threadId": thread})["thread"])
    if state != "READY" and not (state == "RUNNING" and force):
        return f"skipped: chat is {state.lower()}"
    return pane, view, thread


def relaunch_pane(session: str, pane_id: str, thread: str | None, force: bool) -> str:
    """Restart one pane's Codex view on the installed launcher; the chat itself is untouched."""
    first = relaunch_check(session, pane_id, thread, force)
    if isinstance(first, str):
        return first
    # The user may have switched chats, or the process may have changed, while earlier panes
    # were handled. Decide again on fresh facts and act only if nothing moved.
    again = relaunch_check(session, pane_id, first[2], force)
    if isinstance(again, str):
        return again
    pane, view, thread = again
    if (pane["terminal_id"], pane.get("terminal_title_stripped"), view["pid"]) != (
            first[0]["terminal_id"], first[0].get("terminal_title_stripped"), first[1]["pid"]):
        return "skipped: the view changed while it was being checked"
    os.kill(view["pid"], signal.SIGTERM)
    if not wait_for_shell(session, pane_id, starting=True):
        return f"error: view stopped but the pane did not return to a shell; chat {thread}"
    if command(session, "pane", "get", pane_id)["pane"]["terminal_id"] != pane["terminal_id"]:
        return f"error: the pane was replaced after its view stopped; reopen chat {thread}"
    launch_client(session, pane_id, thread)
    for _ in range(100):
        if thread_panes(panes(session), session).get(thread, {}).get("pane_id") == pane_id:
            return f"relaunched {thread}"
        time.sleep(.1)
    return f"error: the new view did not register; inspect the pane, chat {thread}"


@herdr_app.command("relaunch")
def relaunch_command(
    pane: str = typer.Option("", help="One pane ID; default is every pane with a view."),
    thread: str = typer.Option("", help="With --pane: the chat a view started without an ID "
                                        "is showing."),
    force: bool = typer.Option(False, help="Also restart views whose chat has a turn running."),
    session: str = SESSION,
) -> None:
    """Restart Codex views so they run the installed launcher. Chats keep running.

    A view is a window onto a chat held by the shared owner, so its chat continues while the
    view restarts. A view is restarted only when its start command or --thread names the
    chat, the terminal title singles that chat out, and the chat is idle (or, with --force,
    running). Views waiting on an approval or an answer are always left alone.
    """
    try:
        if thread and not pane:
            raise SharedServerError("--thread names the chat in one pane; pass --pane too")
        config = load_config()
        if not config.shared_server.enabled:
            raise SharedServerError("Shared attachment is disabled; views cannot be reopened.")
        # A stopped view can only come back if the installed launcher may attach to the
        # running owner. Find that out before anything is stopped.
        ensure_server(agentroute_home() / "bin" / "codex-bin", config)
        targets = [pane] if pane else [p["pane_id"] for p in panes(session)]
        failed = False
        for target in targets:
            try:
                status = relaunch_pane(session, target, str(UUID(thread)) if thread else None,
                                       force)
            except chats.ERRORS + (KeyError,) as error:
                status = f"error: {error}"
            failed = failed or status.startswith("error")
            if pane or status != "no view of this shared owner":
                typer.echo(f"{target}  {status}")
        if failed:
            raise typer.Exit(1)
    except chats.ERRORS as error:
        raise typer.BadParameter(str(error)) from error


def launch_thread(argv: list[str]) -> str | None:
    if "resume" in argv:
        index = argv.index("resume")
        if index + 1 < len(argv):
            try:
                return str(UUID(argv[index + 1]))
            except ValueError:
                pass
    return None


def run_in_pane(argv: list[str], environment: dict[str, str]) -> int:
    """Run Codex in this pane, recognisable to Herdr, and keep its chat identity current."""
    pane = environment["HERDR_PANE_ID"]
    session = environment.get("HERDR_SESSION") or SESSION
    # Herdr cannot recognise Codex behind this launcher. The hint makes it label the pane and
    # read working, blocked and idle from the screen. That is right for whichever chat the
    # view shows and cannot go stale, so AgentRoute reports no state of its own.
    environment = {**environment, "HERDR_AGENT": "codex"}
    stopped = threading.Event()
    follower = watcher = result = None
    if environment.get("AGENTROUTE_LOCAL_SERVER_SOCKET"):
        follower = PaneFollower(session, pane, launch_thread(argv))
        watcher = threading.Thread(target=follower.run, args=(stopped,), daemon=True)
        watcher.start()
    try:
        result = subprocess.run(argv, env=environment, check=False).returncode
        return result
    finally:
        stopped.set()
        if watcher:
            watcher.join(timeout=12)
        if result == 0 and follower and follower.binding_path:
            follower.binding_path.unlink(missing_ok=True)
