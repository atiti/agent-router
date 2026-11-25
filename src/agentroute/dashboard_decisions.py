"""Explicit dashboard operator decisions for revision-bound work-packet questions."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from contextlib import contextmanager

from . import dashboard_actions as actions
from . import dashboard_packet_routes as routes
from . import waits
from .shared_server import RpcClient, socket_path, state_dir
from .supervisor_inbox import atomic_write, supervisors

CHOICES = {"approve", "deny", "comment"}

# LocalAuthentication is a system-owned prompt. A dashboard capability alone is
# insufficient: another process in this account could start its own dashboard.
AUTH_SOURCE = """
import Foundation
import LocalAuthentication
let data = FileHandle.standardInput.readDataToEndOfFile()
guard let reason = String(data: data, encoding: .utf8), !reason.isEmpty else { exit(2) }
let context = LAContext()
guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: nil) else { exit(2) }
context.localizedCancelTitle = "Cancel decision"
let done = DispatchSemaphore(value: 0)
var accepted = false
context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { success, _ in
    accepted = success
    done.signal()
}
guard done.wait(timeout: .now() + 70) == .success else { exit(2) }
exit(accepted ? 0 : 1)
"""


def confirm_operator(key: tuple[str, str, str], scope: dict, choice: str) -> None:
    if sys.platform != "darwin":
        raise actions.ActionError("Browser decisions need macOS owner confirmation. "
                                  "Use the session for this decision.", 409)
    reason = (
        f"AgentRoute · {choice.capitalize()} packet revision {scope['revision']} "
        f"({scope['scope_digest'][:12]}). {scope['proposal']}"
    )
    folder = state_dir()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with os.fdopen(os.open(folder / "dashboard-confirm.lock", os.O_CREAT | os.O_RDWR,
                               0o600), "a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise actions.ActionError("Finish the current system confirmation first.",
                                          409) from error
            result = subprocess.run(
                ["/usr/bin/swift", "-e", AUTH_SOURCE], input=reason.encode(),
                capture_output=True, timeout=100, check=False,
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise actions.ActionError("The system could not confirm your decision. Nothing was sent.",
                                  409) from error
    if result.returncode != 0:
        raise actions.ActionError("The system did not confirm your decision. Nothing was sent.",
                                  409)



def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def directory(key: tuple[str, str, str]):
    return routes.record_path(key, "decisions").with_suffix("")


def recorded(key: tuple[str, str, str]) -> list[dict]:
    existing = [routes.read_record(path) for path in directory(key).glob("*.json")]
    return sorted((entry for entry in existing if entry),
                  key=lambda entry: entry["created_at"], reverse=True)


def show_receipt(record: dict) -> dict:
    sent = bool(record.get("turn_id"))
    return {
        "id": record["id"], "state": "delivered" if sent else "fired",
        "delivery": {"state": "delivered" if sent else "unknown", "mode": "operator_decision"},
        "created_at": record["created_at"], "recipient": record["recipient"],
        "decision": record["decision"], "slot": record["slot"],
    }


def history(key: tuple[str, str, str]) -> list[dict]:
    return [show_receipt(r) for r in recorded(key)[:12]]


def explicit_proposal(wait: dict) -> str:
    proposal = wait.get("proposal")
    if proposal is None:
        # The tracker previously recorded some proposals inline after a dated receipt.
        # A receipt ID or a bare description of the blocker cannot stand in for a proposal.
        match = re.fullmatch(r"\d{4}-\d{2}-\d{2} request: (.+)", wait["question_ref"])
        proposal = match[1] if match else ""
    if not isinstance(proposal, str) or not proposal.strip() or len(proposal) > 420:
        return ""
    if any(unicodedata.category(char).startswith("C") for char in proposal):
        return ""
    return proposal.strip()


def request_scope(key: tuple[str, str, str], item: dict, slot: object = None) -> dict:
    if item.get("status") in {"done", "cancelled"}:
        return {"eligible": False, "reason": "This outcome is closed."}
    step = item.get("next_step") or {}
    options = (("step", step.get("wait")), ("outcome", item.get("outcome_wait")))
    for name, wait in options:
        if slot is not None and slot != name:
            continue
        if not isinstance(wait, dict) or (
            (step.get("state") if name == "step" else wait.get("state")) != "awaiting_user"
        ):
            continue
        fields = ("question_ref", "reason", "resume_when", "owner")
        if not all(isinstance(wait.get(field), str) and wait[field].strip()
                   and len(wait[field]) <= 4000 for field in fields):
            continue
        action = step.get("action") or item.get("next_action") or ""
        if not isinstance(action, str) or len(action) > 4000:
            continue
        scope = {
            "eligible": True, "revision": item["revision"], "slot": name,
            "scope": wait["question_ref"], "step": action, "reason": wait["reason"],
            "owner": wait["owner"], "resume_when": wait["resume_when"],
            "proposal": explicit_proposal(wait),
        }
        scope["scope_digest"] = digest([key, scope, item.get("assignment")])
        return scope
    return {"eligible": False, "reason": "No explicit work packet question is recorded here. "
                                        "Open the session to answer a live prompt."}


def named_operator(owner: str, names: list[str]) -> bool:
    return owner.strip().casefold() in {name.strip().casefold() for name in names}


def preview(key: tuple[str, str, str], names: list[str]) -> dict:
    item = actions.read_item(key)
    scope = request_scope(key, item)
    if scope["eligible"]:
        if not named_operator(scope["owner"], names):
            return {"eligible": False, "reason": "This question names another decision maker."}
        issue = decision_issue(key, item, scope)
        scope.update(can_decide=not issue, decision_issue=issue)
        scope["recorded"] = next(
            (show_receipt(r) for r in recorded(key)
             if r["scope_digest"] == scope["scope_digest"] and r["decision"] != "comment"),
            None,
        )
    return scope


@contextmanager
def lock(key: tuple[str, str, str]):
    path = directory(key)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path / "submit.lock", os.O_CREAT | os.O_RDWR, 0o600), "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def native_owner(target: str) -> bool:
    if routes.core_member(target):
        return True
    for name in (state_dir() / "herdr-teams").glob("*/team.json"):
        team = routes.read_record(name)
        if team and target in supervisors(team).values():
            return True
    return False


def decision_issue(key: tuple[str, str, str], item: dict, scope: dict) -> str | None:
    if not scope["proposal"]:
        return "This question needs a written proposal before it can be approved here."
    if sys.platform != "darwin":
        return "Browser decisions require macOS owner confirmation. Use the session instead."
    try:
        target = routes.route(key, item)["thread"]
        if target and native_owner(target):
            return "Answer in Herdr for this managed session."
    except routes.HERDR_ERRORS:
        return "The packet session could not be verified."
    return None


def operator_text(key: tuple[str, str, str], scope: dict, choice: str, request_id: str) -> str:
    statement = (
        "I approve the exact proposal below. Do only what that proposal covers."
        if choice == "approve" else
        "I deny the exact proposal below. Keep that action on hold."
    )
    return (
        f"{statement}\n\nDashboard decision {request_id} for {'/'.join(key)}, "
        f"{scope['slot']} hold, revision {scope['revision']}, "
        f"scope {scope['scope_digest']}.\n"
        "Verify the packet's current revision, owner and scope before using my decision. "
        "If anything material changed, ask me to review the new proposal. Honor other "
        "approval gates and Codex tool permissions. Preserve any existing claim.\n"
        f"Proposal: {json.dumps(scope['proposal'], ensure_ascii=False)}"
    )


def submit(data: dict, names: list[str]) -> dict:
    key = actions.packet_key(data)
    request_id = routes.thread_id(data.get("request_id"))
    choice, message = data.get("decision"), data.get("message", "")
    slot, revision = data.get("slot"), data.get("revision")
    if (not request_id or not isinstance(choice, str) or choice not in CHOICES
            or slot not in ("step", "outcome") or type(revision) is not int
            or not isinstance(message, str) or len(message) > actions.MAX_MESSAGE
            or (choice == "comment" and not message.strip())
            or (choice != "comment" and message.strip())):
        raise actions.ActionError("Choose a decision and review the current proposal.")
    fingerprint = digest([key, choice, message, revision, slot, data.get("scope_digest")])
    with lock(key):
        previous = recorded(key)
        for entry in previous:
            if entry["id"] == request_id:
                if entry["fingerprint"] != fingerprint:
                    raise actions.ActionError("This decision reference already exists.", 409)
                return show_receipt(entry)
        item = actions.check_current(key, revision)
        scope = request_scope(key, item, slot)
        if not scope["eligible"] or scope["scope_digest"] != data.get("scope_digest"):
            raise actions.ActionError("The proposal changed. Review it again before deciding.",
                                      409)
        if choice != "comment" and any(
            entry["decision"] != "comment" and entry["scope_digest"] == scope["scope_digest"]
            for entry in previous
        ):
            raise actions.ActionError("A decision is already recorded for this revision.", 409)
        if not named_operator(scope["owner"], names):
            raise actions.ActionError("This question names another decision maker.", 409)
        if choice == "comment":
            return actions.submit({**data, "message": message, "recipient": None},
                                  decision_comment=True)
        if any(w["spec"].get("dashboard_request") == request_id for w in waits.load()):
            raise actions.ActionError("This decision reference was already used by a reply.", 409)
        issue = decision_issue(key, item, scope)
        if issue:
            raise actions.ActionError(issue, 409)
        current = routes.route(key, item)
        confirm_operator(key, scope, choice)
        if actions.check_current(key, revision) != item or routes.route(key, item) != current:
            raise actions.ActionError("The packet or session changed. Review it again.", 409)
        try:
            route = routes.ensure_thread(key, item, actions.packets_root())
            target = route["thread"]
            if native_owner(target):
                raise actions.ActionError("Answer in Herdr for this managed session.", 409)
            return deliver(key, scope, target, choice, request_id, fingerprint, item)
        except actions.ActionError:
            raise
        except routes.RouteError as error:
            raise actions.ActionError(str(error), 409) from error
        except routes.HERDR_ERRORS as error:
            raise actions.ActionError("The session could not accept the decision. Reopen it "
                                      "before trying again.", 503) from error


def deliver(key, scope, target, choice, request_id, fingerprint, item) -> dict:
    with RpcClient(socket_path(), name="agentroute_dashboard_operator", timeout=5) as client:
        params = {"threadId": target}
        thread = client.request("thread/read", params)["thread"]
        if thread.get("id") != target or thread.get("archived"):
            raise actions.ActionError("The recorded session is unavailable or archived.", 409)
        if thread["status"]["type"] == "notLoaded":
            client.request("thread/resume", params)
            thread = client.request("thread/read", params)["thread"]
        if thread.get("id") != target or thread.get("status", {}).get("type") != "idle":
            raise actions.ActionError("This session is busy. Finish its current turn or respond "
                                      "to its live prompt in Herdr, then try again.", 409)
        if (actions.check_current(key, scope["revision"]) != item
                or routes.route(key, item)["thread"] != target or native_owner(target)):
            raise actions.ActionError("The packet or session changed. Review it again.", 409)
        record = {
            "id": request_id, "decision": choice, "fingerprint": fingerprint,
            "scope_digest": scope["scope_digest"], "recipient": target,
            "slot": scope["slot"], "revision": scope["revision"], "scope": scope,
            "created_at": time.time(), "confirmation": "macos-device-owner",
        }
        file = directory(key) / f"{request_id}.json"
        atomic_write(file, record)
        try:
            response = client.request("turn/start", {
                **params,
                "input": [{"type": "text",
                           "text": operator_text(key, scope, choice, request_id)}],
            })
            if turn := (response.get("turn") or {}).get("id"):
                record["turn_id"] = turn
                atomic_write(file, record)
        except routes.RPC_ERRORS:
            pass  # The native turn may have been admitted. Never send another on retry.
        return show_receipt(record)
