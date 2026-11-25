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
from .shared_server import RpcClient, SharedServerError, socket_path, state_dir

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


def title_label(thread: dict) -> str:
    """How Codex names a thread in its terminal title: its name, or its ID when unnamed."""
    label = str(thread.get("name") or "").strip() or str(thread.get("id") or "")
    if len(label) <= TITLE_THREAD_CHARS:
        return label
    return label[:TITLE_THREAD_CHARS - 3] + "..."


def title_shows(title: str, thread: dict) -> bool:
    label = title_label(thread)
    return bool(label) and (label in title.split(" | ") or title.startswith(f"{label} | "))


def shown_thread(client: RpcClient, pane: dict, started: str | None) -> str | None:
    """The chat a pane's Codex is showing, or None when two chats fit equally well.

    A view can move to another chat (/new, /resume, a fork) without telling anyone, so the
    thread it was started with is only the first candidate. The terminal title is the one
    thing the view publishes about itself: it carries the thread's name, or its ID when the
    thread is unnamed. Two chats with the same name in the same directory cannot be told
    apart this way, so a view that moves between them keeps its earlier identity.
    """
    title = pane.get("terminal_title_stripped") or ""

    def read(thread_id: str) -> dict | None:
        try:
            return client.request("thread/read", {"threadId": thread_id})["thread"]
        except (SharedServerError, KeyError, TypeError):
            return None

    current = read(started) if started and title else None
    if not title or (current and title_shows(title, current)):
        return started
    loaded = client.request("thread/loaded/list", {}).get("data") or []
    matches = [thread for thread in map(read, loaded) if thread and title_shows(title, thread)]
    if not matches:
        # No loaded chat is named in the title: it is still being drawn, or the user's
        # title layout leaves the thread out. The launch command is the best evidence.
        return started
    if len(matches) > 1:
        directory = pane.get("foreground_cwd") or pane.get("cwd")
        matches = [thread for thread in matches if thread.get("cwd") == directory]
    return str(UUID(matches[0]["id"])) if len(matches) == 1 else None


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
            thread = shown_thread(client, info, self.thread)
        self._checked, self._checked_at = seen, time.monotonic()
        if thread is None:
            return  # Unknown: leave the last verified identity rather than record a guess.
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
    """The Codex client process in the foreground of a pane, if there is one."""
    info = command(session, "pane", "process-info", "--pane", pane)["process_info"]
    return next((process for process in info.get("foreground_processes", [])
                 if process.get("name") == "codex-bin"), None)


def relaunch_pane(session: str, pane: dict, force: bool) -> str:
    """Restart one pane's Codex view on the installed launcher; the chat itself is untouched."""
    view = codex_view(session, pane["pane_id"])
    if not view:
        return "no Codex view"
    saved = next((record["thread"] for record in saved_panes(session)
                  if record["terminal_id"] == pane["terminal_id"]), None)
    with RpcClient(socket_path(), timeout=5) as client:
        thread = shown_thread(client, pane, native_thread(pane) or saved)
        current = client.request("thread/read", {"threadId": thread})["thread"] if thread else {}
    # Reopening the wrong chat would be worse than leaving an old view, so the title has to
    # name this chat outright; the follower's fallback to the launch thread is not enough.
    if not title_shows(pane.get("terminal_title_stripped") or "", current):
        return "skipped: cannot tell which chat this view shows"
    state = chats.status(current)
    if state in {"APPROVAL", "INPUT"} or (state == "RUNNING" and not force):
        return f"skipped: chat is {state.lower()}"
    os.kill(view["pid"], signal.SIGTERM)
    if not wait_for_shell(session, pane["pane_id"], starting=True):
        return f"error: view stopped but the pane did not return to a shell; chat {thread}"
    launch_client(session, pane["pane_id"], thread)
    return f"relaunched {thread}"


@herdr_app.command("relaunch")
def relaunch_command(
    pane: str = typer.Option("", help="One pane ID; default is every pane with a Codex view."),
    force: bool = typer.Option(False, help="Also restart views whose chat has a turn running."),
    session: str = SESSION,
) -> None:
    """Restart Codex views so they run the installed launcher. Chats keep running.

    A view is only a window onto a chat held by the shared owner, so a turn in progress
    continues while its view restarts. Views waiting on an approval or an answer are left
    alone, as are views whose chat cannot be identified from the terminal title.
    """
    try:
        targets = [p for p in panes(session) if not pane or p["pane_id"] == pane]
        for target in targets:
            try:
                status = relaunch_pane(session, target, force)
            except chats.ERRORS + (KeyError,) as error:
                status = f"error: {error}"
            if status != "no Codex view" or pane:
                typer.echo(f'{target["pane_id"]}  {status}')
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
