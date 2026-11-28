"""Durable launch records for new shared sessions requested from Voice."""

from __future__ import annotations

import fcntl
import hashlib
import os
import time
from contextlib import contextmanager
from pathlib import Path

from . import dashboard_packet_routes as routes
from . import herdr_cli as herdr
from . import waits
from . import zellij_cli as chats
from .shared_server import RpcClient, socket_path, state_dir
from .supervisor_inbox import atomic_write
from .voice_evidence import conversation_url, reply_prompt


@contextmanager
def launch_record(request_id: str):
    if routes.thread_id(request_id) != request_id:
        raise ValueError("Use an exact Voice creation request ID.")
    path = state_dir() / "voice-launches" / f"{request_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield path, routes.read_record(path)


def _create_thread(path: Path, record: dict) -> None:
    # App Server allocates the ID. An interrupted request with no ID cannot be
    # replayed safely. Retain its receipt for operator reconciliation.
    with RpcClient(socket_path(), name="agentroute_voice_create", timeout=10) as client:
        record["creation_state"] = "unconfirmed"
        atomic_write(path, record)
        thread = client.request("thread/start", {"cwd": record["cwd"]})["thread"]
        identifier = routes.thread_id(thread.get("id"))
        if not identifier:
            return
        record.update(thread_id=identifier, creation_state="created")
        atomic_write(path, record)


def _first_instruction(path: Path, record: dict) -> None:
    if record.get("wait_id"):
        return
    identifier = record["id"]
    existing = [wait for wait in waits.load()
                if wait["spec"].get("dashboard_request") == identifier]
    if existing:
        if len(existing) != 1 or existing[0]["thread_id"] != record["thread_id"]:
            raise ValueError("The first instruction receipt conflicts with this launch.")
        wait = existing[0]
    else:
        attempted = record.get("enqueue_started_at", time.time())
        if time.time() - attempted >= waits.RETAIN_FINISHED_SECONDS:
            return
        record["enqueue_started_at"] = attempted
        atomic_write(path, record)
        wait = waits.register(
            record["thread_id"], "external",
            {"dashboard_request": identifier, "dashboard_fingerprint": record["fingerprint"],
             "dashboard_kind": "owner", "voice_reply_marker": identifier},
            note=("First instruction for this shared session from the Voice operator. "
                  "Read result.evidence and apply existing permissions and holds. "
                  + reply_prompt(identifier)),
            fired_payload=record["message"], timeout=waits.MAX_TIMEOUT, cwd=record["cwd"],
        )
    record["wait_id"] = wait["id"]
    record.pop("message", None)
    atomic_write(path, record)


def _matching_pane(panes: list[dict], record: dict) -> str | None:
    matches = [pane["pane_id"] for pane in panes
               if herdr.native_thread(pane) == record.get("thread_id")]
    if len(matches) > 1:
        raise ValueError("Several Herdr panes report this thread.")
    return next(iter(matches), None)


def _attach(path: Path, record: dict) -> None:
    session = record["herdr_session"]
    try:
        panes = herdr.panes(session)
    except routes.HERDR_ERRORS:
        if record["herdr_state"] in {"pending", "offline"}:
            record["herdr_state"] = "offline"
            atomic_write(path, record)
        return
    existing = _matching_pane(panes, record)
    if existing:
        record.update(pane=existing, herdr_state="attached")
    elif record["herdr_state"] in {"pending", "offline"}:
        with RpcClient(socket_path(), name="agentroute_voice_create", timeout=5) as client:
            thread = client.request("thread/read", {
                "threadId": record["thread_id"], "includeTurns": False,
            })["thread"]
        if thread.get("id") != record["thread_id"] or thread.get("archived"):
            raise ValueError("The created session is unavailable.")
        record["herdr_state"] = "unconfirmed"
        atomic_write(path, record)
        record["pane"] = herdr.open_thread(
            {**thread, "name": record["title"]}, session,
            workspace_label=record["namespace"], verify_native=True,
        )
        record["herdr_state"] = "attached"
    atomic_write(path, record)


def read_status(record: dict) -> dict:
    target = record.get("thread_id")
    wait_id = record.get("wait_id")
    delivery = "unconfirmed" if record.get("enqueue_started_at") else record["creation_state"]
    return {
        "id": f"create:{record['id']}", "source": "app_server", "recipient": target,
        "session": record["title"], "namespace": record["namespace"],
        "conversation_url": conversation_url(target), "creation_state": record["creation_state"],
        "receipt_state": delivery,
        "instruction_receipt_id": wait_id,
        "herdr_state": record["herdr_state"], "herdr_pane": record.get("pane"),
        "herdr_session": record["herdr_session"],
    }


def status(request_id: str, *, retry: bool = False) -> dict:
    with launch_record(request_id) as (path, record):
        if not record:
            raise ValueError("This Voice launch receipt is unavailable.")
        if retry:
            return _advance(path, record)
        if record["herdr_state"] == "unconfirmed":
            try:
                if pane := _matching_pane(herdr.panes(record["herdr_session"]), record):
                    record.update(pane=pane, herdr_state="attached")
                    atomic_write(path, record)
            except routes.HERDR_ERRORS:
                pass
        return read_status(record)


def create(request_id: str, namespace: str, title: str, cwd: Path, message: str) -> dict:
    intent = {
        "namespace": namespace, "title": title, "cwd": str(cwd),
        "fingerprint": hashlib.sha256(message.encode()).hexdigest(),
    }
    with launch_record(request_id) as (path, record):
        if record and any(record.get(key) != value for key, value in intent.items()):
            raise ValueError("This Voice request already belongs to a different launch.")
        if not record:
            record = {
                **intent, "id": request_id, "creation_state": "pending",
                "message": message, "herdr_state": "pending",
                "herdr_session": os.environ.get("HERDR_SESSION") or herdr.SESSION,
            }
            atomic_write(path, record)
        return _advance(path, record)


def _advance(path: Path, record: dict) -> dict:
    try:
        if record["creation_state"] == "pending":
            _create_thread(path, record)
        if target := record.get("thread_id"):
            if not record.get("configured"):
                with chats.operation_lock():
                    chats.remember_chat_namespace(target, record["namespace"])
                with RpcClient(socket_path(), name="agentroute_voice_create", timeout=5) as client:
                    client.request("thread/name/set", {
                        "threadId": target, "name": record["title"],
                    })
                record["configured"] = True
                atomic_write(path, record)
            _first_instruction(path, record)
            if record.get("wait_id"):
                waits.ensure_daemon()
    except (*routes.RPC_ERRORS, waits.WaitError):
        return read_status(routes.read_record(path))
    if record.get("wait_id"):
        try:
            _attach(path, record)
        except routes.HERDR_ERRORS:
            return read_status(routes.read_record(path))
    return read_status(record)
