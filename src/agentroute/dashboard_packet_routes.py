"""Resolve an exact packet conversation and expose it through Herdr."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from typer import BadParameter
from websockets.exceptions import WebSocketException

from . import herdr_cli as herdr
from . import herdr_team as team
from .config import codex_home
from .shared_server import RpcClient, SharedServerError, socket_path, state_dir
from .supervisor_inbox import atomic_write

RPC_ERRORS = (SharedServerError, OSError, TimeoutError, WebSocketException)


class RouteError(ValueError):
    pass


def thread_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        canonical = str(UUID(value))
    except ValueError:
        return None
    return canonical if canonical == value.lower() else None


def record_path(key: tuple[str, str, str], kind: str) -> Path:
    digest = hashlib.sha256("/".join(key).encode()).hexdigest()
    return state_dir() / "dashboard-packets" / kind / f"{digest}.json"


def read_record(file: Path) -> dict:
    try:
        value = json.loads(file.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise RouteError("The saved packet conversation needs reconciliation.")
    return value


def legacy_candidates(key: tuple[str, str, str], assignment: dict) -> set[str]:
    manual = read_record(record_path(key, "owners"))
    candidates = set()
    if (manual.get("packet") == list(key) and manual.get("assignment") == assignment
            and manual.get("evidence") and (thread := thread_id(manual.get("thread")))):
        candidates.add(thread)
    for file in (codex_home() / "worker-goal-bindings").glob("*.json"):
        try:
            binding = read_record(file)
        except (OSError, ValueError):
            continue
        if (binding.get("namespace"), binding.get("packet"), binding.get("item"),
                binding.get("session")) != (*key, assignment.get("session")):
            continue
        thread = thread_id(binding.get("thread_id"))
        if binding.get("version") == 1 and thread and file.stem == thread:
            candidates.add(thread)
    return candidates


def route(key: tuple[str, str, str], item: dict) -> dict:
    assignment = item.get("assignment") or {}
    owner = assignment.get("session")
    exact = thread_id(owner)
    if exact:
        return {"kind": "owner", "thread": exact, "assignment": owner}
    if owner:
        candidates = legacy_candidates(key, assignment)
        if len(candidates) > 1:
            raise RouteError("More than one thread is bound to this assignment. Reconcile the "
                             "recorded owner before sending.")
        if candidates:
            return {"kind": "owner", "thread": next(iter(candidates)), "assignment": owner}
    saved = read_record(record_path(key, "review"))
    if saved and (saved.get("packet") != list(key) or not thread_id(saved.get("thread"))):
        raise RouteError("Review conversation creation is unconfirmed. Reconcile the saved "
                         "launch before retrying.")
    return {"kind": "review", "thread": saved.get("thread"), "assignment": owner}


@contextmanager
def route_lock(key: tuple[str, str, str]):
    lock = record_path(key, "review").with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(lock, os.O_CREAT | os.O_RDWR, 0o600), "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def ensure_thread(key: tuple[str, str, str], item: dict, root: Path) -> dict:
    with route_lock(key):
        current = route(key, item)
        if current["thread"]:
            return current
        path = record_path(key, "review")
        payload = {"packet": list(key), "state": "creating"}
        with RpcClient(socket_path(), name="agentroute_dashboard", timeout=5) as client:
            # Record intent before a request that could succeed even if its response is lost.
            atomic_write(path, payload)
            response = client.request("thread/start", {"cwd": str(root.resolve())})
            thread = thread_id((response.get("thread") or {}).get("id"))
            if not thread:
                raise RouteError("Review conversation creation is unconfirmed.")
            atomic_write(path, {**payload, "thread": thread, "state": "created"})
            client.request("thread/name/set", {
                "threadId": thread, "name": f"Packet review · {'/'.join(key)}"[:160],
            })
        return {**current, "thread": thread}


def core_member(target: str) -> tuple[str, str, str] | None:
    members = []
    for file in (state_dir() / "herdr-teams").glob("*/team.json"):
        data = read_record(file)
        if data.get("id") != file.parent.name:
            continue
        for member, record in data.get("members", {}).items():
            if record.get("thread") == target:
                members.append((data["id"], member, data["tui_session"]))
        if data.get("anchor_thread") == target:
            bound = (data["id"], "supervisor", data["tui_session"])
            if bound not in members:
                members.append(bound)
    if len(members) > 1:
        raise RouteError("This thread has several Herdr team members; reconcile its bindings.")
    return members[0] if members else None


def show_in_herdr(target: str, key: tuple[str, str, str], kind: str) -> dict:
    session = os.environ.get("HERDR_SESSION") or herdr.SESSION
    member_binding = core_member(target)
    if member_binding:
        team_id, member, session = member_binding
        herdr.start_herdr(session)
        pane = team.open_member_view(team_id, member, exact_thread=target)
        herdr.focus_pane(session, herdr.command(session, "pane", "get", pane)["pane"])
    else:
        with RpcClient(socket_path(), name="agentroute_dashboard", timeout=5) as client:
            result = client.request("thread/read", {"threadId": target, "includeTurns": False})
            thread = result["thread"]
        if thread.get("id") != target or thread.get("archived"):
            raise RouteError("The exact session is unavailable or archived.")
        herdr.start_herdr(session)
        pane = herdr.open_thread(
            thread, session,
            workspace_label=f"{key[0]} · Packet review" if kind == "review" else None,
            verify_native=True,
        )
    return {"pane": pane, "session": session, "recipient": target, "kind": kind}


HERDR_ERRORS = RPC_ERRORS + (BadParameter, KeyError, ValueError, RuntimeError,
                            subprocess.SubprocessError)
