"""A persistent terminal cockpit over the existing shared Codex owner.

Zellij owns terminal views; app-server owns chats. Listing/focusing never resumes
a chat or acquires its writer lock. Only an explicit open/new creates a client.
"""

from __future__ import annotations

import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text
from websockets.exceptions import WebSocketException

from .config import agentroute_home, load_config, save_config
from .server_cli import _start
from .shared_server import RpcClient, SharedServerError, socket_path, state_dir
from .work_cli import pages

zellij_app = typer.Typer(no_args_is_help=True, help="Tile, browse, and focus shared Codex chats.")
group_app = typer.Typer(no_args_is_help=True, help="Set project roots for Zellij tab sorting.")
zellij_app.add_typer(group_app, name="group")
SESSION = "agentroute"
TAB_SESSIONS = "Sessions"
TAB_MARKSTER = "Markster"
TAB_PRIVATE = "Private"
PROJECT_GROUPS_FILE = "zellij-project-groups.json"
ERRORS = (OSError, ValueError, SharedServerError, WebSocketException, subprocess.SubprocessError)


def executable(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise SharedServerError(f"{name} is missing from PATH; install it first")
    return path


def action(session: str, *args: str, timeout: int = 15) -> str:
    result = subprocess.run(
        [executable("zellij"), "--session", session, "action", *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode:
        raise SharedServerError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def panes(session: str) -> list[dict]:
    return json.loads(action(session, "list-panes", "--all", "--json"))


def tabs(session: str) -> list[dict]:
    result = json.loads(action(session, "list-tabs", "--json"))
    if isinstance(result, dict):
        result = result.get("tabs", [])
    if not isinstance(result, list):
        raise SharedServerError("Zellij returned an unexpected tab list")
    return result


def tab_name(tab: dict) -> str:
    return str(tab.get("name") or tab.get("tab_name") or "")


def tab_identifier(tab: dict) -> str | None:
    value = tab.get("tab_id", tab.get("id"))
    return str(value) if value is not None else None


def ensure_tab(session: str, name: str) -> str:
    for tab in tabs(session):
        if tab_name(tab).casefold() == name.casefold():
            identifier = tab_identifier(tab)
            if identifier is None:
                raise SharedServerError(f"Zellij did not return an ID for the {name!r} tab")
            return identifier
    identifier = action(session, "new-tab", "--name", name, "--no-focus")
    if not identifier:
        raise SharedServerError(f"Zellij did not return an ID for the new {name!r} tab")
    return identifier


def project_groups_path() -> Path:
    return agentroute_home() / PROJECT_GROUPS_FILE


def load_project_groups() -> list[dict[str, str]]:
    try:
        value = json.loads(project_groups_path().read_text())
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as error:
        raise SharedServerError(f"Could not read project tab rules: {error}") from error
    if not isinstance(value, dict) or not isinstance(value.get("roots", []), list):
        raise SharedServerError("Project tab rules have an invalid format")
    rules = []
    for item in value.get("roots", []):
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or item.get("group") not in {"markster", "private"}):
            raise SharedServerError("Project tab rules have an invalid entry")
        rules.append({"path": item["path"], "group": item["group"]})
    return rules


def save_project_groups(rules: list[dict[str, str]]) -> None:
    path = project_groups_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"version": 1, "roots": rules}, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


@lru_cache(maxsize=512)
def _origin_is_markster(cwd: str) -> bool:
    """Read the local Git origin only; this performs no network access."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "config", "--local", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode:
        return False
    remote = result.stdout.strip().casefold()
    if "://" in remote:
        path = urlsplit(remote).path
    else:
        path = remote.split(":", 1)[-1]
    owner_repo = [part.removesuffix(".git") for part in path.strip("/").split("/")[:2]]
    if len(owner_repo) != 2:
        return False
    owner, repository = owner_repo
    return (owner.startswith("markster") or repository.startswith("markster")
            or (owner == "atiti" and repository == "agent-router"))


def project_group(cwd: str | Path) -> str:
    """Choose the Markster or Private tab from an explicit root or local project identity."""
    project = Path(cwd).expanduser().resolve()
    matching = []
    for rule in load_project_groups():
        root = Path(rule["path"]).expanduser().resolve()
        if project == root or root in project.parents:
            matching.append((len(root.parts), rule["group"]))
    if matching:
        return max(matching, key=lambda item: item[0])[1].title()
    if any(part.casefold() == "agent-router" or part.casefold().startswith("markster")
           for part in project.parts):
        return TAB_MARKSTER
    if _origin_is_markster(str(project)):
        return TAB_MARKSTER
    return TAB_PRIVATE


def set_project_group(directory: Path, group: str) -> None:
    canonical = group.casefold()
    if canonical not in {"markster", "private"}:
        raise typer.BadParameter("group must be 'markster' or 'private'")
    root = directory.expanduser().resolve()
    if not root.is_dir():
        raise typer.BadParameter(f"project root is not a directory: {root}")
    rules = [rule for rule in load_project_groups()
             if Path(rule["path"]).expanduser().resolve() != root]
    rules.append({"path": str(root), "group": canonical})
    save_project_groups(rules)


@group_app.command("set")
def group_set_command(group: str, directory: Path) -> None:
    """Route a project directory and all its children to Markster or Private."""
    try:
        set_project_group(directory, group)
    except (OSError, SharedServerError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(f"{directory.expanduser().resolve()} → {group.title()}")


@group_app.command("remove")
def group_remove_command(directory: Path) -> None:
    """Remove an explicit project rule and return that root to automatic detection."""
    try:
        root = directory.expanduser().resolve()
        rules = [rule for rule in load_project_groups()
                 if Path(rule["path"]).expanduser().resolve() != root]
        save_project_groups(rules)
    except (OSError, SharedServerError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(f"Removed project tab override for {root}")


@group_app.command("list")
def group_list_command() -> None:
    """Show explicit project roots and the automatic fallback rule."""
    try:
        rules = load_project_groups()
    except SharedServerError as error:
        raise typer.BadParameter(str(error)) from error
    if not rules:
        typer.echo("No project overrides. Markster repositories route to Markster; "
                   "other roots route to Private.")
        return
    for rule in sorted(rules, key=lambda item: item["path"]):
        typer.echo(f"{rule['group'].title():8} {rule['path']}")


def session_state(session: str) -> str | None:
    """Return whether a named Zellij session is running, exited, or absent."""
    result = subprocess.run(
        [executable("zellij"), "list-sessions", "--no-formatting"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode:
        raise SharedServerError(result.stderr.strip() or result.stdout.strip())
    for line in result.stdout.splitlines():
        if line == session or line.startswith(f"{session} ["):
            return "exited" if "(EXITED - attach to resurrect)" in line else "running"
    return None


def thread_panes(items: list[dict]) -> dict[str, dict]:
    result = {}
    for pane in items:
        if pane.get("is_plugin") or pane.get("exited"):
            continue
        command = pane.get("terminal_command") or ""
        # The immutable command launched by us survives terminal title changes.
        match = re.search(r"\bzellij client --thread ([0-9a-f-]{36})\b", command)
        if match:
            result[match[1]] = pane
    return result


def sessions(client: RpcClient, limit: int = 100) -> list[dict]:
    recent = client.request("thread/list", {
        "limit": limit, "sortKey": "updated_at", "modelProviders": [], "useStateDbOnly": True,
    })["data"]
    by_id = {thread["id"]: thread for thread in recent}
    # Loaded chats remain visible even when older than the recent-history window.
    for thread_id in pages(client, "thread/loaded/list", {}):
        by_id[thread_id] = client.request("thread/read", {"threadId": thread_id})["thread"]
    return list(by_id.values())


def title(thread: dict) -> str:
    return " ".join((thread.get("name") or thread.get("preview") or "Untitled").split())[:160]


def status(thread: dict) -> str:
    current = thread.get("status", {})
    flags = current.get("activeFlags", [])
    if "waitingOnApproval" in flags:
        return "APPROVAL"
    if "waitingOnUserInput" in flags:
        return "INPUT"
    return {"active": "RUNNING", "idle": "READY", "systemError": "ERROR"}.get(
        current.get("type"), "SAVED"
    )


def resolve(client: RpcClient, query: str) -> dict:
    try:
        thread_id = str(UUID(query))
    except ValueError:
        candidates = sessions(client, 100)
        matches = [t for t in candidates if t["id"].startswith(query)]
        if not matches:
            matches = [t for t in candidates if query.casefold() in title(t).casefold()]
        if len(matches) != 1:
            raise SharedServerError(
                f"Expected one chat, found {len(matches)}. Use its full UUID from `ar list`."
            ) from None
        return matches[0]
    return client.request("thread/read", {"threadId": thread_id})["thread"]


def runner() -> list[str]:
    # Use the same Python/package installation as this process, including venvs.
    return [sys.executable, "-m", "agentroute.cli", "zellij"]


@contextmanager
def operation_lock():
    import fcntl

    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "zellij.lock").open("a") as handle:
        os.chmod(handle.name, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def open_thread(thread: dict, session: str) -> str:
    with operation_lock():
        items = panes(session)
        existing = thread_panes(items).get(thread["id"])
        if existing:
            pane_id = f"terminal_{existing['id']}"
            if existing.get("tab_id") is not None:
                action(session, "go-to-tab-by-id", str(existing["tab_id"]))
            action(session, "focus-pane-id", pane_id)
            return pane_id
        destination = project_group(thread["cwd"])
        tab = ensure_tab(session, destination)
        action(session, "go-to-tab-by-id", tab)
        created = action(session, "new-pane", "--name", title(thread), "--cwd", thread["cwd"],
                         "--", *runner(), "client", "--thread", thread["id"])
        tile_panes(session, tab)
        action(session, "focus-pane-id", created)
        return created


def pane_mover_plugin_path() -> Path:
    return Path(__file__).resolve().parent / "plugins" / "pane_mover.wasm"


def move_thread_pane(thread: dict, destination: str, session: str) -> str:
    """Move an existing Codex terminal into a group tab without restarting it."""
    group = {TAB_MARKSTER.casefold(): TAB_MARKSTER,
             TAB_PRIVATE.casefold(): TAB_PRIVATE}.get(destination.casefold())
    if group is None:
        raise SharedServerError("Tab must be 'Markster' or 'Private'.")

    with operation_lock():
        pane = thread_panes(panes(session)).get(thread["id"])
        if pane is None:
            raise SharedServerError(
                "That chat has no open terminal pane. Use `ar open <chat>` first."
            )
        destination_id = ensure_tab(session, group)
        pane_id = f"terminal_{pane['id']}"
        if str(pane.get("tab_id")) == destination_id:
            action(session, "go-to-tab-by-id", destination_id)
            action(session, "focus-pane-id", pane_id)
            return f"Already in {group}; focused {pane_id}."

        plugin = pane_mover_plugin_path()
        if not plugin.is_file():
            raise SharedServerError(
                "The Zellij pane mover plugin is missing from this AgentRoute install. "
                "Reinstall AgentRoute to restore its packaged plugin."
            )
        action(
            session,
            "pipe",
            "--plugin", f"file:{plugin}",
            "--name", "agentroute-move-pane",
            "--floating-plugin", "true",
            "--plugin-title", "AgentRoute pane mover",
            "--", f"{pane['id']} {destination_id}",
            timeout=90,
        )

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            moved = thread_panes(panes(session)).get(thread["id"])
            if moved and str(moved.get("tab_id")) == destination_id:
                tile_panes(session, destination_id)
                action(session, "go-to-tab-by-id", destination_id)
                action(session, "focus-pane-id", f"terminal_{moved['id']}")
                return f"Moved {title(thread)!r} to {group}; focused terminal_{moved['id']}."
            time.sleep(0.1)

        raise SharedServerError(
            "Zellij did not confirm the move. If its pane-mover permission prompt is open, "
            "approve 'Change application state' and run the command again."
        )


def tile_panes(session: str, tab_id: str | None = None) -> None:
    items = panes(session)
    if tab_id is None:
        focused = next((p for p in items if p.get("is_focused") and not p.get("is_plugin")), None)
        tab_id = str(focused["tab_id"] if focused else next(p["tab_id"] for p in items))
    # Swap layouts rearrange the existing pane objects. Unlike override-layout,
    # they cannot spawn duplicate commands and can target a detached tab by ID.
    action(session, "next-swap-layout", "--tab-id", str(tab_id))


def setup_workspace_tabs(session: str) -> None:
    """Name the board tab and create the two grouped chat tabs without moving panes."""
    items = panes(session)
    available = tabs(session)
    board_pane = next((p for p in items if not p.get("is_plugin") and
                       "zellij board" in (p.get("terminal_command") or "")), None)
    sessions_tab = next((t for t in available
                         if tab_name(t).casefold() == TAB_SESSIONS.casefold()), None)
    if board_pane and sessions_tab is None and board_pane.get("tab_id") is not None:
        action(session, "rename-tab-by-id", str(board_pane["tab_id"]), TAB_SESSIONS)
    ensure_tab(session, TAB_MARKSTER)
    ensure_tab(session, TAB_PRIVATE)


def host_path(session: str) -> Path:
    # Hashing also keeps arbitrary session names out of filesystem paths.
    import hashlib

    key = hashlib.sha256(session.encode()).hexdigest()[:12]
    return state_dir() / f"zellij-{key}.json"


def register_target(thread: str) -> Path:
    """Per-thread address lets hooks in the shared owner find this terminal view."""
    session = os.environ["ZELLIJ_SESSION_NAME"]
    pane_id = int(os.environ["ZELLIJ_PANE_ID"])
    path = state_dir() / "zellij-targets" / f"{thread}.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        host = json.loads(host_path(session).read_text())
    except (OSError, ValueError):
        host = {}
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({**host, "session": session, "pane_id": pane_id,
                                     "thread": thread}))
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def remember_host(session: str) -> None:
    if os.environ.get("ZELLIJ"):
        return
    terminal = os.environ.get("TERM_PROGRAM", "")
    terminal = {"iTerm.app": "iTerm2", "Apple_Terminal": "Terminal"}.get(terminal, terminal)
    try:
        tty = os.ttyname(sys.stdin.fileno())
    except OSError:
        tty = None
    path = host_path(session)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps({"terminal": terminal, "tty": tty}))
    path.chmod(0o600)
    for target in (state_dir() / "zellij-targets").glob("*.json"):
        try:
            address = json.loads(target.read_text())
            if address.get("session") == session:
                address.update(terminal=terminal, tty=tty)
                temporary = target.with_suffix(f".{os.getpid()}.tmp")
                temporary.write_text(json.dumps(address))
                temporary.chmod(0o600)
                temporary.replace(target)
        except (OSError, ValueError):
            continue


def notify(thread: dict, pane: dict, session: str, event: str) -> None:
    binary = shutil.which("overwatchr")
    if not binary:
        return
    try:
        host = json.loads(host_path(session).read_text())
    except (OSError, ValueError):
        host = {}
    args = [binary, event, "--agent", f"codex-{thread['id']}", "--project",
            Path(thread["cwd"]).name, "--title", title(thread),
            "--zellij-session", session, "--zellij-pane-id", str(pane["id"]),
            "--zellij-thread-id", thread["id"]]
    for field in ("terminal", "tty"):
        if host.get(field):
            args.extend([f"--{field}", host[field]])
    subprocess.run(args, capture_output=True, timeout=5, check=True)


def notify_changes(threads: list[dict], mapped: dict[str, dict], previous: dict[str, str],
                   session: str) -> dict[str, str]:
    current = {}
    for thread in threads:
        thread_id = thread["id"]
        if thread_id not in mapped:
            continue
        value = status(thread)
        current[thread_id] = value
        old = previous.get(thread_id)
        if value == old:
            continue
        # Initial READY is a chat waiting at a prompt, not a newly completed turn.
        event = None
        if value in {"INPUT", "APPROVAL", "ERROR"} or (value == "READY" and old == "RUNNING"):
            event = "error" if value == "ERROR" else "alert"
        elif value == "RUNNING" and old in {"READY", "INPUT", "APPROVAL", "ERROR"}:
            event = "done"  # Overwatchr's done event clears its attention queue.
        if event:
            notify(thread, mapped[thread_id], session, event)
    return current


def table(threads: list[dict], mapped: dict[str, dict], selected: int = -1,
          compact: bool = False) -> Table:
    result = Table(title="AgentRoute · shared sessions", expand=True)
    headings = ("", "State", "Project / chat") if compact else ("", "State", "Project / chat", "ID")
    for heading in headings:
        result.add_column(heading, no_wrap=heading in {"", "State", "ID"})
    for index, thread in enumerate(threads):
        value = status(thread)
        color = {"RUNNING": "green", "APPROVAL": "yellow", "INPUT": "yellow",
                 "ERROR": "red", "READY": "cyan"}.get(value, "dim")
        label = f"[{project_group(thread['cwd'])}] {Path(thread['cwd']).name}\n{title(thread)}"
        columns = ["›" if index == selected else ("●" if thread["id"] in mapped else ""),
                   Text(value, style=color), Text(label, overflow="ellipsis", no_wrap=True)]
        if not compact:
            columns.append(thread["id"][:8])
        result.add_row(*columns, style="bold" if index == selected else "")
    return result


@contextmanager
def keyboard():
    import termios
    import tty

    fd = sys.stdin.fileno()
    settings = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, settings)


@zellij_app.command("board")
def board(session: str = SESSION, limit: int = typer.Option(30, min=1, max=100)) -> None:
    """Live board: j/k select, Enter open/focus, t tile, / filter, q exit."""
    if not sys.stdin.isatty():
        raise typer.BadParameter("board needs a terminal; use `ar list` for plain output")
    selected, query, filtering, message = 0, "", False, ""
    previous: dict[str, str] = {}
    console = Console()
    try:
        try:
            setup_workspace_tabs(session)
        except ERRORS as error:
            message = str(error)
        with keyboard(), Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                try:
                    with RpcClient(socket_path(), name="agentroute_zellij_board") as client:
                        threads = sessions(client, limit)
                    mapped = thread_panes(panes(session))
                    previous = notify_changes(threads, mapped, previous, session)
                    threads.sort(key=lambda t: (status(t) not in {"APPROVAL", "INPUT", "ERROR"},
                                                t["id"] not in mapped, -t.get("updatedAt", 0)))
                    visible = [t for t in threads if query.casefold() in
                               (title(t) + t["cwd"] + t["id"]).casefold()]
                    selected = min(selected, max(0, len(visible) - 1))
                    # Keep the selected row visible when the board is a narrow sidebar.
                    height = max(1, (console.size.height - 8) // 3)
                    offset = max(0, selected - height + 1)
                    from rich.console import Group
                    footer = Text(f"j/k select · Enter focus · t tile · / search · q quit\n"
                                  f"Filter: {query}{'_' if filtering else ''}\n{message}")
                    live.update(Group(table(visible[offset:offset + height], mapped,
                                            selected - offset, compact=console.size.width < 75),
                                      footer), refresh=True)
                except ERRORS as error:
                    live.update(Text(f"Shared owner/board unavailable: {error}\nq quit; retrying…"),
                                refresh=True)
                    visible = []
                if not select.select([sys.stdin], [], [], 2)[0]:
                    continue
                key = os.read(sys.stdin.fileno(), 32).decode(errors="replace")
                if filtering:
                    if key in {"\n", "\r", "\x1b"}:
                        filtering = False
                    elif key in {"\x7f", "\b"}:
                        query = query[:-1]
                    elif key.isprintable():
                        query += key
                    selected = 0
                elif key in {"q", "\x03"}:
                    return
                elif key in {"j", "\x1b[B"}:
                    selected += 1
                elif key in {"k", "\x1b[A"}:
                    selected = max(0, selected - 1)
                elif key == "/":
                    filtering = True
                elif key in {"\n", "\r"} and visible:
                    try:
                        message = open_thread(visible[selected], session)
                    except ERRORS as error:
                        message = str(error)
                elif key == "t":
                    try:
                        if visible:
                            destination = project_group(visible[selected]["cwd"])
                            tab = ensure_tab(session, destination)
                            tile_panes(session, tab)
                            message = f"Tiled {destination} tab; all terminal processes retained."
                        else:
                            tile_panes(session)
                            message = "Tiled current tab; all terminal processes retained."
                    except ERRORS as error:
                        message = str(error)
    except KeyboardInterrupt:
        pass


@zellij_app.command("list")
def list_command(as_json: bool = typer.Option(False, "--json"),
                 limit: int = typer.Option(30, min=1, max=100)) -> None:
    """Read recent and loaded chats without resuming or changing them."""
    try:
        with RpcClient(socket_path()) as client:
            threads = sessions(client, limit)
        if as_json:
            typer.echo(json.dumps(threads, indent=2))
        else:
            Console().print(table(threads, {}))
    except ERRORS as error:
        raise typer.BadParameter(str(error)) from error


@zellij_app.command("open")
def open_command(query: str, session: str = SESSION) -> None:
    """Open a chat by full ID, recent ID prefix, or unambiguous title substring."""
    try:
        with RpcClient(socket_path()) as client:
            thread = resolve(client, query)
        typer.echo(open_thread(thread, session))
    except ERRORS as error:
        raise typer.BadParameter(str(error)) from error


@zellij_app.command("move")
def move_command(query: str, tab: str = typer.Option(..., "--tab"),
                 session: str = SESSION) -> None:
    """Move an open chat pane into the Markster or Private tab without restarting it."""
    try:
        with RpcClient(socket_path()) as client:
            thread = resolve(client, query)
        typer.echo(move_thread_pane(thread, tab, session))
    except ERRORS as error:
        raise typer.BadParameter(str(error)) from error


@zellij_app.command("focus")
def focus_command(query: str = typer.Argument("board"), session: str = SESSION) -> None:
    """Focus an existing pane; never opens another client or resumes a chat."""
    try:
        items = panes(session)
        if query == "board":
            target = next((p for p in items if not p.get("is_plugin") and
                           "zellij board" in (p.get("terminal_command") or "")), None)
        else:
            with RpcClient(socket_path()) as client:
                thread = resolve(client, query)
            target = thread_panes(items).get(thread["id"])
        if not target:
            raise SharedServerError("No open pane. Run `ar open <chat>` first.")
        pane_id = f"terminal_{target['id']}"
        current = os.environ.get("ZELLIJ_SESSION_NAME")
        if current and current != session:
            action(current, "switch-session", session, "--pane-id", pane_id)
        else:
            if target.get("tab_id") is not None:
                action(session, "go-to-tab-by-id", str(target["tab_id"]))
            action(session, "focus-pane-id", pane_id)
            if not current and sys.stdin.isatty():
                remember_host(session)
                os.execvp("zellij", ["zellij", "attach", session])
        typer.echo(f"Focused {session}/{pane_id}")
    except ERRORS as error:
        raise typer.BadParameter(str(error)) from error


@zellij_app.command("new")
def new_command(directory: Path = typer.Option(Path.cwd(), "--dir", exists=True, file_okay=False),
                name: str = "", session: str = SESSION) -> None:
    """Create a shared chat in a project directory and open its terminal view."""
    try:
        panes(session)  # Fail before creating a chat if the workspace does not exist.
        with RpcClient(socket_path(), timeout=30) as client:
            thread = client.request("thread/start", {"cwd": str(directory.resolve())})["thread"]
            if name:
                client.request("thread/name/set", {"threadId": thread["id"], "name": name})
                thread["name"] = name
        typer.echo(open_thread(thread, session))
    except ERRORS as error:
        raise typer.BadParameter(str(error)) from error


@zellij_app.command("client", hidden=True)
def client_command(thread: str = typer.Option(...)) -> None:
    """Launch an explicitly shared TUI. Its immutable command identifies the pane."""
    thread = str(UUID(thread))
    if not load_config().shared_server.enabled:
        raise typer.BadParameter("Shared attachment is disabled; run `agentroute server enable`.")
    command = [sys.executable, "-m", "agentroute.cli", "launch-codex", "--", "resume", thread]
    target = register_target(thread)
    try:
        result = subprocess.run(command, check=False)
    finally:
        target.unlink(missing_ok=True)
    if result.returncode:
        typer.echo("Could not attach. A chat owned by an older standalone CLI/Desktop must finish "
                   "and close there before opening here. Writer locks were preserved.", err=True)
    raise typer.Exit(result.returncode)


@zellij_app.command("tile")
def tile_command(session: str = SESSION) -> None:
    """Cycle the current tab through adaptive layouts, retaining every terminal process."""
    try:
        tile_panes(session)
    except ERRORS as error:
        raise typer.BadParameter(str(error)) from error


@zellij_app.command("start")
def start_command(session: str = SESSION, background: bool = False) -> None:
    """Start/reuse the owner and persistent workspace, then attach to it."""
    try:
        _start()
        settings = load_config()
        if not settings.shared_server.enabled:
            settings.shared_server.enabled = True
            save_config(settings)
        remember_host(session)
        config = state_dir() / "zellij.kdl"
        config.write_text('default_mode "locked"\nauto_layout true\nsession_serialization true\n'
                          'show_startup_tips false\nshow_release_notes false\n'
                          'keybinds { locked { bind "Ctrl g" { SwitchToMode "Normal"; }; }; }\n')
        state = session_state(session)
        if state is None:
            args = [executable("zellij"), "--config", str(config), "attach",
                    "--create-background", session, "--", *runner(), "board", "--session", session]
            result = subprocess.run(args, capture_output=True, text=True, timeout=20)
            if result.returncode:
                # Another launcher may have created it after our state check.
                state = session_state(session)
                if state is None:
                    raise SharedServerError(result.stderr.strip() or result.stdout.strip()
                                            or f"Could not create Zellij session {session!r}")
            else:
                state = "running"
        if state == "running":
            setup_workspace_tabs(session)
        if background and state == "exited":
            raise SharedServerError(
                f"Zellij session {session!r} is saved but exited; run `ar` to resurrect and attach."
            )
        if not background:
            current = os.environ.get("ZELLIJ_SESSION_NAME")
            if current:
                if current != session:
                    if state == "exited":
                        raise SharedServerError(
                            f"Zellij session {session!r} is exited; use `ar` from a terminal "
                            "outside Zellij to resurrect it."
                        )
                    action(current, "switch-session", session)
            else:
                attach = ["zellij", "--config", str(config), "attach"]
                if state == "exited":
                    attach.append("--force-run-commands")
                attach.append(session)
                os.execvp("zellij", attach)
        typer.echo(f"Workspace ready. Attach with: zellij attach {shlex.quote(session)}")
    except ERRORS as error:
        raise typer.BadParameter(str(error)) from error


@zellij_app.command("install")
def install_command(bin_dir: Path | None = typer.Option(None)) -> None:
    """Install ar and focus commands alongside the existing AgentRoute launcher."""
    directory = bin_dir or agentroute_home() / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    launcher = shlex.quote(str(agentroute_home() / "bin" / "agentroute"))
    scripts = {
        "ar": ('if [ "$#" -eq 0 ]; then set -- start; fi\n'
               f'exec {launcher} zellij "$@"\n'),
        "focus": f'exec {launcher} zellij focus "$@"\n',
    }
    for name, body in scripts.items():
        path = directory / name
        marker = "# AgentRoute Zellij cockpit"
        if path.exists() and marker not in path.read_text():
            raise typer.BadParameter(f"Refusing to replace an unrelated command: {path}")
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(f"#!/bin/sh\n{marker}\nset -eu\n{body}")
        temporary.chmod(0o755)
        temporary.replace(path)
    typer.echo(f"Installed ar and focus in {directory}. Ensure this directory is on PATH.")
