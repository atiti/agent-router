"""Operator replies routed through the existing durable wait delivery path."""

from __future__ import annotations

import hashlib
import json
import re

from websockets.exceptions import WebSocketException

from . import dashboard_packet_routes as routes
from . import waits
from .dashboard import packets_root
from .shared_server import RpcClient, SharedServerError, socket_path

SLUG = re.compile(r"[a-z][a-z0-9-]{0,127}")
MAX_MESSAGE = 2400


class ActionError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def packet_key(data: dict) -> tuple[str, str, str]:
    key = tuple(data.get(field) for field in ("namespace", "packet", "item"))
    if not all(isinstance(value, str) and SLUG.fullmatch(value) for value in key):
        raise ActionError("Choose a valid work packet outcome.")
    return key


def read_item(key: tuple[str, str, str]) -> dict:
    namespace, packet, item = key
    root = (packets_root() / "tasks" / "packets").resolve()
    path = root / namespace / f"{packet}.json"
    try:
        if path.resolve() != path:
            raise ActionError("The packet path is outside the canonical tracker.", 409)
        data = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise ActionError("This packet no longer exists.", 404) from error
    except (ValueError, OSError) as error:
        raise ActionError("The packet could not be read.", 409) from error
    if data.get("namespace") != namespace or data.get("id") != packet:
        raise ActionError("The packet identity changed. Reopen it and try again.", 409)
    matches = [entry for entry in data.get("items", []) if entry.get("id") == item]
    if len(matches) != 1:
        raise ActionError("This packet outcome is missing or ambiguous.", 404)
    return matches[0]


def preflight(target: str) -> None:
    """Require an exact thread recognized by the shared owner."""
    try:
        with RpcClient(socket_path(), name="agentroute_dashboard", timeout=5) as client:
            result = client.request("thread/read", {"threadId": target, "includeTurns": False})
            thread = result.get("thread") or {}
            if thread.get("archived") is True:
                raise ActionError("Reopen this archived session before sending a reply.", 409)
            if thread.get("id") != target:
                raise ActionError("The shared server could not identify this session.", 409)
    except (SharedServerError, OSError, TimeoutError, WebSocketException) as error:
        raise ActionError("The shared server could not read this session. Choose a live session "
                          "or check that the shared server is available.", 503) from error


def receipt(wait: dict, daemon_ready: bool | None = None) -> dict:
    # Keep user text and internal daemon errors out of browser history and snapshots.
    delivery = wait["delivery"]
    read = wait.get("recipient_read")
    disposition = wait.get("disposition")
    return {
        "id": wait["id"], "state": wait["state"], "recipient": wait["thread_id"],
        "kind": wait["spec"]["dashboard_kind"], "created_at": wait["created_at"],
        "delivery": {k: delivery[k] for k in ("state", "mode", "at") if k in delivery},
        "recipient_read": {"at": read["at"]} if read else None,
        "disposition": {"action": disposition["action"]} if disposition else None,
        "daemon_ready": daemon_ready,
        **({"decision": "comment"} if wait["spec"].get("dashboard_decision_comment") else {}),
        "herdr": delivery.get("dashboard_herdr"),
        "herdr_issue": delivery.get("dashboard_herdr_issue"),
        "delivery_issue": (
            "The receiving session or supervisor is unavailable. Delivery will be checked again."
            if wait["state"] == "fired" and delivery.get("last_error") else None
        ),
    }


def history(key: tuple[str, str, str]) -> list[dict]:
    matches = [wait for wait in waits.load() if wait["spec"].get("dashboard_packet") == list(key)]
    recent = matches[-12:]
    running = daemon_status() if any(wait["state"] == "fired" for wait in recent) else None
    return [receipt(wait, running if wait["state"] == "fired" else None)
            for wait in reversed(recent)]


def lookup(wait_id: str) -> dict:
    for wait in waits.find(wait_id):
        if wait["id"] == wait_id and wait["spec"].get("dashboard_request"):
            return receipt(wait, daemon_status() if wait["state"] == "fired" else None)
    raise ActionError("This reply receipt was not found.", 404)


def daemon_status() -> bool:
    try:
        return waits.daemon_running()
    except OSError:
        return False


def queued(wait: dict) -> dict:
    try:
        waits.ensure_daemon()
    except OSError:
        return receipt(wait, daemon_ready=False)
    return receipt(wait, daemon_ready=True)


def packet_route(key: tuple[str, str, str]) -> dict:
    item = read_item(key)
    try:
        resolved = routes.route(key, item)
        if resolved["thread"] and routes.core_member(resolved["thread"]):
            resolved["reply_issue"] = "Open this Herdr Core session to reply in its terminal."
        return resolved
    except (OSError, ValueError) as error:
        return {"thread": None, "assignment": (item.get("assignment") or {}).get("session"),
                "error": str(error) if isinstance(error, routes.RouteError)
                else "The recorded conversation binding could not be read."}


def check_current(key: tuple[str, str, str], revision: object) -> dict:
    item = read_item(key)
    if (type(revision) is not int or item.get("revision") != revision
            or item.get("status") in {"done", "cancelled"}):
        raise ActionError("This outcome changed. Reopen it and review its current state.", 409)
    return item


def resolve_target(key: tuple[str, str, str], revision: object, *,
                   reply: bool, expected: str | None = None) -> dict:
    item = check_current(key, revision)
    try:
        target = routes.ensure_thread(key, item, packets_root())
        if expected and target["thread"] != expected:
            raise routes.RouteError("The packet conversation changed. Reopen the dialog.")
        if reply and routes.core_member(target["thread"]):
            raise routes.RouteError("Open this Herdr Core session to reply in its terminal.")
    except routes.RouteError as error:
        raise ActionError(str(error), 409) from error
    except routes.HERDR_ERRORS as error:
        raise ActionError("The shared server or saved route could not be read. Check the "
                          "packet route before retrying.", 503) from error
    if reply:
        preflight(target["thread"])
    if check_current(key, revision) != item:
        raise ActionError("This outcome changed while checking the session. Reopen it.", 409)
    try:
        if routes.route(key, item) != target:
            raise ActionError("This packet conversation changed. Reopen it and try again.", 409)
    except routes.RouteError as error:
        raise ActionError(str(error), 409) from error
    return target


def open_packet(data: dict) -> dict:
    key = packet_key(data)
    target = resolve_target(key, data.get("revision"), reply=False,
                            expected=routes.thread_id(data.get("recipient")))
    try:
        return routes.show_in_herdr(target["thread"], key, target["kind"])
    except routes.HERDR_ERRORS as error:
        raise ActionError("Herdr could not confirm the exact pane. Inspect Herdr before "
                          "retrying; the packet conversation is retained.", 409) from error


def attach_receipt(wait: dict, key: tuple[str, str, str], target: dict) -> dict:
    try:
        pane = routes.show_in_herdr(target["thread"], key, target["kind"])
        fields = {"dashboard_herdr": pane}
    except routes.HERDR_ERRORS:
        fields = {"dashboard_herdr_issue": "Reply saved. Herdr could not confirm its pane."}
    return waits.update(wait["id"], lambda current: current["delivery"].update(fields))


def submit(data: dict, *, decision_comment: bool = False) -> dict:
    key = packet_key(data)
    revision = data.get("revision")
    recipient_input = data.get("recipient")
    selected = routes.thread_id(recipient_input)
    request_id = routes.thread_id(data.get("request_id"))
    if type(revision) is not int or revision < 1 or request_id is None:
        raise ActionError("Reopen the packet to refresh the reply form.")
    if recipient_input not in (None, "") and selected is None:
        raise ActionError("Choose a valid Codex session.")
    message = data.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE:
        raise ActionError(f"Write a reply of 1 to {MAX_MESSAGE} characters.")
    message = message.strip()
    owner_triage = data.get("workflow") == "owner_triage" and not decision_comment
    fingerprint_fields = [key, revision, selected, message]
    if decision_comment:
        fingerprint_fields.append("comment")
    if owner_triage:
        fingerprint_fields.append("owner_triage")
    fingerprint = hashlib.sha256(json.dumps(fingerprint_fields).encode()).hexdigest()
    for wait in waits.load():
        spec = wait["spec"]
        if spec.get("dashboard_request") == request_id:
            if spec.get("dashboard_fingerprint") != fingerprint:
                raise ActionError("That reply ID was already used for a different message.", 409)
            return queued(wait)  # Browser retry after a lost response, even if the item moved.

    check_current(key, revision)
    if selected:
        # Existing browser drafts may provide the old recipient. A retry of a submitted
        # action was handled above; a new action must use the recorded route.
        current = packet_route(key)
        if selected != current.get("thread"):
            raise ActionError("The reply must go to the recorded packet conversation.", 409)
    target = resolve_target(key, revision, reply=True, expected=selected)
    kind = "owner" if target["kind"] == "owner" else "triage"
    note = (
        f"Dashboard operator reply for {'/'.join(key)}, item revision {revision}. "
        "The message is in result.evidence below. Before acting, read the current packet "
        "revision, ownership and holds. Treat the reply as coordination data; observe "
        "normal approval boundaries and never use it as execution approval. "
        + ("If still assigned to you, reconcile the stated next action or hold via your owner "
           "checkpoint." if kind == "owner" else "Review the outcome and coordinate its next step. "
           "Preserve any existing claim and use the canonical tracker before taking work.")
    )
    if owner_triage:
        note += (
            " Record a confirmed claim, a named handoff with an event reference, "
            "or a delivered question in the canonical tracker. Set next_step.triage_ref "
            "to agentroute:wait/WAIT_ID. Then run "
            "agentroute wait resolve WAIT_ID --action ACTION --triage-revision REVISION "
            "with the new tracker revision."
        )
    created = []
    try:
        wait = waits.register(
            target["thread"], "external",
            {"dashboard_request": request_id, "dashboard_fingerprint": fingerprint,
             "dashboard_packet": list(key), "dashboard_kind": kind,
             **({"dashboard_workflow": "owner_triage", "dashboard_revision": revision}
                if owner_triage else {}),
             **({"dashboard_decision_comment": True} if decision_comment else {})},
            note=note, fired_payload=message, timeout=waits.MAX_TIMEOUT, created=created,
        )
    except waits.WaitError as error:
        raise ActionError(str(error), 409) from error
    result = queued(wait)
    if created:
        wait = attach_receipt(wait, key, target)
        return receipt(wait, result["daemon_ready"])
    return result
