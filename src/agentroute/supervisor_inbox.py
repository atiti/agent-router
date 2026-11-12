"""Single-writer supervisor inbox projections consumed at native safe boundaries.

The team ledger is authoritative. A pane prompt is only an optional notification;
its success never acknowledges or dispositions the coordination event.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import time
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import typer

from .config import codex_home


def api():
    from . import herdr_team
    return herdr_team


def path_for(thread: str) -> Path:
    return codex_home() / "supervisor-inbox" / f"{UUID(thread)}.json"


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    try:
        with temporary.open("x") as stream:
            os.chmod(temporary, 0o600)
            json.dump(payload, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def supervisors(data: dict) -> dict[str, str]:
    result = {name: record["thread"] for name, record in data["members"].items()
              if record.get("spec", {}).get("role") == "supervisor" and record.get("thread")}
    if data.get("anchor_thread") and "supervisor" not in result:
        result["supervisor"] = data["anchor_thread"]
    return result


def bind_anchor(data: dict) -> None:
    """Migrate old anchored teams from the verified Herdr client registry, once."""
    if not data.get("anchor_terminal") or data.get("anchor_thread"):
        return
    h = api()
    directory = h.tui.registry_path(data["tui_session"], str(UUID(int=0))).parent
    candidates = []
    for path in directory.glob("*.json"):
        try:
            record = json.loads(path.read_text())
            if record.get("terminal_id") == data["anchor_terminal"]:
                os.kill(record["pid"], 0)
                candidates.append(str(UUID(path.stem)))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if len(candidates) == 1:
        data["anchor_thread"] = candidates[0]
    elif len(candidates) > 1:
        raise ValueError("Multiple live Codex threads for supervisor anchor; reconcile binding")


def is_supervisor(data: dict, member: str) -> bool:
    return (data["members"].get(member, {}).get("spec", {}).get("role") == "supervisor"
            or member == "supervisor" and bool(data.get("anchor_terminal")))


def event(message_id: str, message: dict, team: str) -> dict:
    return {"id": message_id, "state": "persisted",
            "actionable": message.get("actionable", True),
            "kind": "health" if message_id.startswith("health-") else "coordination",
            "message": message["text"], "source": {"team": team, "member": message["from"]},
            "created_at": message.get("created_at", time.time()),
            "delivery": {"state": "pending"}}


def bounded_text(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    return encoded[:limit].decode("utf-8", errors="ignore")


def project_event(entry: dict) -> dict:
    """Transport summaries only; complete messages and evidence remain in the team ledger."""
    result = {k: entry[k] for k in ("id", "state", "actionable", "kind", "source", "created_at",
                                   "delivery")}
    result["message"] = bounded_text(entry["message"], 1024)
    if entry.get("resolves_event"):
        result["resolves_event"] = entry["resolves_event"]
    if entry.get("wait"):
        result["wait"] = {key: bounded_text(value, 1024) for key, value in entry["wait"].items()}
    return result


def enrollment_binding(data: dict) -> dict:
    return {"threads": supervisors(data), "namespace": data["namespace"],
            "packet": data["packet"], "item": data["item"]}


def validate_enrollment(data: dict) -> None:
    enrollment = data.get("inbox_consumer", {})
    if enrollment.get("binding") and enrollment["binding"] != enrollment_binding(data):
        raise typer.BadParameter("Inbox enrollment binding changed; coordinate a new team handoff")


def publish(data: dict) -> None:
    """Called under the team lock after ledger fsync; idempotently repair projections."""
    if data.get("inbox_consumer", {}).get("version") != 1:
        return
    for member, thread in supervisors(data).items():
        path = path_for(thread)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError("Supervisor inbox directory and snapshot must not be symlinks")
        os.chmod(path.parent, 0o700)
        children = []
        for name, record in data["members"].items():
            if record.get("spec", {}).get("role") == "supervisor":
                continue
            if record.get("cleanup", {}).get("state") == "archived":
                continue  # Completed history remains in the team ledger, outside pending children.
            observation = record.get("terminal_observation", {})
            observed = observation.get("at")
            fresh = isinstance(observed, (int, float)) and 0 <= time.time()-observed <= 120
            if record.get("cleanup", {}).get("state") == "archived":
                state, source, reconcile = "complete", "canonical-completion", False
            elif fresh:
                terminal_state = observation.get("terminal_state")
                reported = observation.get("reported_state")
                state = ("stopped" if terminal_state == "finished" else
                         "unknown" if terminal_state != "live" else
                         "waiting" if reported in {"blocked", "idle", "done"} else "running")
                source, reconcile = "herdr-core", state in {"unknown", "stopped"}
            else:
                state, source, reconcile = "unknown", "ledger-reconciliation", True
            children.append({"id": name, "state": state, "observed_at": observed,
                             "source": source, "reconcile_required": reconcile,
                             "observation_ttl_seconds": 120})
        for candidate in data.get("packet_readiness", {}).get("candidates", []):
            children.append({"id": f"packet:{data['packet']}/{candidate}@"
                      f"{data['packet_readiness'].get('epochs', {}).get(candidate, 0)}",
                "state": "queued",
                "observed_at": data["packet_readiness"].get("observed_at"),
                "source": "canonical-scheduler", "reconcile_required": True,
                "observation_ttl_seconds": 120})
        payload = {"version": 1, "role": "supervisor", "thread_id": str(UUID(thread)),
                   "team": data["id"], "member": member, "namespace": data["namespace"],
                   "packet": data["packet"], "item": data["item"],
                   "objective": bounded_text(data["members"].get(member, {}).get("spec", {}).get(
                       "task", data.get("brief", "")), 8192),
                   "live_children": children[:128], "events": []}
        def valid_wait(entry):
            wait = entry.get("wait") or {}
            return (wait.get("owner") in {"user", "external"}
                    and wait.get("delivery_state") == "delivered"
                    and bool(wait.get("question_ref")) and bool(wait.get("resume_condition")))

        pending = [message["coordination_event"] for message in data["messages"].values()
                   if message["member"] == member and message.get("coordination_event")
                   and (message["coordination_event"].get("wait") or
                        message["coordination_event"]["state"] != "dispositioned"
                        and message["coordination_event"]["actionable"])]
        actionable = [entry for entry in pending if not valid_wait(entry)]
        waiting = [entry for entry in pending if valid_wait(entry)]
        included_actionable = included_waiting = 0
        for entry in (actionable + waiting)[:128]:
            payload["events"].append(project_event(entry))
            # Reserve bytes for generation and overflow fields; native limit is 262144.
            if len(json.dumps(payload, indent=2).encode()) > 258000:
                payload["events"].pop()
                break
            if valid_wait(entry):
                included_waiting += 1
            else:
                included_actionable += 1
        payload["overflow"] = {"events": len(actionable)-included_actionable,
                               "waiting_events": len(waiting)-included_waiting,
                               "children": max(0, len(children)-128),
                               "diagnostic": "Read full team messages; disposition actionable "
                                             "events to expose remaining pending events"}
        with path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = json.loads(path.read_text()) if path.exists() else {}
            if previous and (previous.get("team"), previous.get("member")) != (data["id"], member):
                raise ValueError("Supervisor thread already belongs to another inbox")
            generation = previous.pop("generation", 0)
            def semantic(value):
                return {**value, "live_children": [
                    {k: v for k, v in child.items() if k != "observed_at"}
                    for child in value.get("live_children", [])]}

            if previous != payload:
                changed = semantic(previous) != semantic(payload)
                atomic_write(path, {**payload, "generation": generation + int(changed)})


def publish_roles(data: dict) -> None:
    """Role context is independent of native inbox enrollment, still ledger-bound."""
    for member, thread in supervisors(data).items():
        path = api().state_dir() / "supervisor-roles" / f"{UUID(thread)}.json"
        atomic_write(path, {"version": 1, "role": "supervisor", "thread_id": str(UUID(thread)),
                            "team": data["id"], "member": member,
                            "objective": data["members"].get(member, {}).get("spec", {}).get(
                                "task", data.get("brief", ""))})


def routing_context(thread: str) -> dict | None:
    """Only a registered supervisor ledger binding can confer a routing role."""
    try:
        role_path = api().state_dir() / "supervisor-roles" / f"{UUID(thread)}.json"
        snapshot = json.loads(role_path.read_text())
        data = api().load(snapshot["team"])
        if (snapshot.get("version") == 1 and snapshot.get("role") == "supervisor"
                and snapshot.get("thread_id") == str(UUID(thread))
                and supervisors(data).get(snapshot["member"]) == str(UUID(thread))):
            return snapshot
    except (OSError, ValueError, KeyError, TypeError, typer.BadParameter):
        pass
    return None


def recipient(data: dict, member: str) -> None:
    expected = supervisors(data).get(member)
    if not expected or expected != os.environ.get("CODEX_THREAD_ID"):
        raise typer.BadParameter("Consume from the bound supervisor Codex session")


def inbox(team: str, member: str = "supervisor") -> None:
    """Consume durable events independently of pane prompt delivery; receipt is not approval."""
    h = api()
    with h.locked(team) as data:
        recipient(data, member)
        h.save(team, data)
        snapshot_path = path_for(supervisors(data)[member])
        visible = {entry["id"] for entry in json.loads(snapshot_path.read_text())["events"]}
        for message_id, message in data["messages"].items():
            entry = message.get("coordination_event")
            if message_id in visible and entry and entry["state"] == "persisted":
                entry.update(state="delivered", delivered_at=time.time())
        h.save(team, data)
        typer.echo(path_for(supervisors(data)[member]).read_text())


def find_event(data: dict, member: str, event_id: str) -> dict:
    recipient(data, member)
    message = data["messages"].get(event_id, {})
    if message.get("member") != member or not message.get("coordination_event"):
        raise typer.BadParameter("Unknown event for this supervisor")
    return message["coordination_event"]


def inbox_ack(team: str, event_id: str, member: str = "supervisor") -> None:
    """Acknowledge a specific consumed event without declaring it resolved."""
    h = api()
    with h.locked(team) as data:
        entry = find_event(data, member, event_id)
        if entry["state"] != "dispositioned":
            entry.update(state="acked", acknowledged_at=time.time())
        h.save(team, data)
        typer.echo(json.dumps(entry))


def disposition(team: str, event_id: str, action: str = typer.Option(...),
                reason: str = typer.Option(...), evidence: list[str] = typer.Option(...),
                member: str = "supervisor", question_ref: str | None = None,
                resume_condition: str | None = None, owner: str | None = None,
                next_action: str | None = None) -> None:
    """Record an evidenced next action or delivered user/external wait, never authority."""
    actions = {"continue", "replan", "owner_handoff", "resolved", "duplicate",
               "awaiting_user", "waiting_external"}
    if (action not in actions or not reason.strip() or not evidence
            or not all(isinstance(ref, str) and ref.strip() for ref in evidence)):
        raise typer.BadParameter("A supported action, reason and evidence are required")
    if action in {"awaiting_user", "waiting_external"} and not (
            question_ref and question_ref.strip()
            and resume_condition and resume_condition.strip()):
        raise typer.BadParameter("A delivered question/reference and resume condition are required")
    if action in {"continue", "replan", "owner_handoff"} and not (
            owner and owner.strip() and next_action and next_action.strip()):
        raise typer.BadParameter("Coordination disposition needs an owner and exact next action")
    h = api()
    with h.locked(team) as data:
        entry = find_event(data, member, event_id)
        proposed = {"action": action, "reason": reason, "evidence": evidence,
                    "owner": owner, "next_action": next_action}
        prior_wait = entry.get("wait")
        wait = ({"owner": "user" if action == "awaiting_user" else "external",
                 "delivery_state": "delivered", "question_ref": question_ref,
                 "resume_condition": resume_condition}
                if action in {"awaiting_user", "waiting_external"} else None)
        if entry.get("disposition") and (entry["disposition"] != proposed or prior_wait != wait):
            raise typer.BadParameter("Event already dispositioned; use a new event")
        entry.update(state="dispositioned", actionable=False, disposition=proposed)
        if wait:
            entry["wait"] = wait
        h.save(team, data)
        typer.echo(json.dumps(entry))


def inbox_resume(team: str, event_id: str, evidence: list[str] = typer.Option(...),
                 member: str = "supervisor") -> None:
    """Resolve a delivered wait using observed user/external evidence; grants no authority."""
    if not evidence or not all(isinstance(ref, str) and ref.strip() for ref in evidence):
        raise typer.BadParameter("Resume evidence is required")
    h = api()
    with h.locked(team) as data:
        entry = find_event(data, member, event_id)
        wait = entry.get("wait")
        if not wait:
            if entry.get("wait_resolution", {}).get("evidence") == evidence:
                typer.echo(json.dumps(entry))
                return
            raise typer.BadParameter("This event has no active wait")
        digest = hashlib.sha256(json.dumps(evidence).encode()).hexdigest()[:16]
        resume_id = f"resume-{event_id[:72]}-{digest}"
        message = {"member": member, "from": member, "state": "queued", "created_at": time.time(),
                   "text": f"Wait {event_id} resolved with evidence {evidence}. "
                           "Reconcile the condition and choose the authorized next action."}
        message["coordination_event"] = event(resume_id, message, team)
        message["coordination_event"].update(kind="wait_resolved", resolves_event=event_id)
        if resume_id in data["messages"]:
            raise typer.BadParameter("Resume event ID collision; inspect the ledger")
        data["messages"][resume_id] = message
        entry["wait_resolution"] = {"wait": entry.pop("wait"), "evidence": evidence,
                                    "at": time.time(), "event_id": resume_id}
        h.save(team, data)
        typer.echo(json.dumps(entry))


def sync_due_reviews(data: dict, items: dict[str, dict], now: float) -> None:
    """Wake only registered structured external/worker waits at their exact review deadline."""
    current = set()
    targets = list(supervisors(data))
    if not targets and is_supervisor(data, "supervisor"):
        targets = ["supervisor"]
    for item_id, item in items.items():
        if item.get("status") in {"done", "cancelled"}:
            continue
        step = item.get("next_step") or {}
        outcome = item.get("outcome_wait") or {}
        waits = []
        if step.get("state") in {"waiting_external", "waiting_worker"}:
            waits.append({**(step.get("wait") or {}), "state": step["state"]})
        if outcome.get("state") in {"waiting_external", "waiting_worker"}:
            waits.append(outcome)
        deadline = item.get("next_review")
        if not waits or deadline is None:
            continue
        diagnostic = None
        try:
            stamp = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                diagnostic = "Registered review deadline has no timezone"
            elif stamp.timestamp() > now:
                continue
        except (AttributeError, TypeError, ValueError):
            diagnostic = "Registered review deadline is malformed"
        identity = json.dumps([data["packet"], item_id, deadline]).encode()
        digest = hashlib.sha256(identity).hexdigest()[:24]
        for target in targets:
            target_digest = hashlib.sha256(target.encode()).hexdigest()[:8]
            key = f"deadline-{digest}-{target_digest}"
            current.add(key)
            if key in data["messages"]:
                continue
            message = {"member": target, "from": "packet-deadline-review", "state": "queued",
                       "created_at": now, "review": {"item": item_id, "deadline": deadline,
                                                     "waits": waits,
                                                     "revision": item.get("revision")},
                       "text": f"Registered review for {data['packet']}/{item_id} at {deadline}. "
                               "Inspect the existing exact watch/source and recorded "
                               "resume condition; prefer an active watch handle. Record findings "
                               "and the next review/owner action. This reminder grants no effect, "
                               "retry, restart, dispatch or approval authority."}
            if diagnostic:
                message["text"] = diagnostic + "; correct the explicit canonical deadline. "
            message["coordination_event"] = event(key, message, data["id"])
            message["coordination_event"]["kind"] = (
                "deadline_diagnostic" if diagnostic else "deadline_review")
            data["messages"][key] = message
    for key, message in data["messages"].items():
        entry = message.get("coordination_event")
        if (entry and entry["kind"] in {"deadline_review", "deadline_diagnostic"}
                and key not in current
                and entry["state"] != "dispositioned"):
            entry.update(state="dispositioned", actionable=False, disposition={
                "action": "superseded", "reason": "Canonical review deadline/wait changed",
                "owner": "packet-deadline-review", "next_action": "Inspect current registered wait",
                "evidence": [f"Canonical packet observed at {now}"]})


def sync_packet_steps(data: dict, items: dict[str, dict], now: float) -> None:
    """Project canonical executable steps, never prose/status guesses or dispatch authority."""
    h = api()
    candidates = json.loads(h.packet_command(data, "next", "--role", "coordinator"))
    if not isinstance(candidates, list):
        raise ValueError("Canonical coordinator queue must use the versioned list contract")
    selected = {row["id"] for row in candidates if row.get("packet") == data["packet"]}
    selected.update(item_id for item_id, item in items.items()
                    if (item.get("next_step") or {}).get("state") == "coordination_required")
    current = set()
    candidate_items = []
    previous_readiness = data.get("packet_readiness", {})
    prior_candidates = set(previous_readiness.get("candidates", []))
    epochs = dict(previous_readiness.get("epochs", {}))
    targets = [name for name in supervisors(data)]
    if not targets and is_supervisor(data, "supervisor"):
        targets = ["supervisor"]
    for item_id in sorted(selected):
        execution = json.loads(h.packet_command(data, "execution", "--packet", data["packet"],
                                               "--item", item_id))
        if (execution.get("protocol_version") != 1
                or execution.get("namespace") != data["namespace"]
                or execution.get("packet") != data["packet"] or execution.get("item") != item_id):
            raise ValueError("Canonical execution identity/version mismatch")
        # A supervisor's assigned active outcome is not its own pending child.
        # Assigned workers remain with their owners unless they explicitly need coordination.
        if not execution.get("actionable") or not (
                execution.get("claimable") and not execution.get("assignment")
                or execution.get("coordination_required")):
            continue
        candidate_items.append(item_id)
        if item_id not in prior_candidates:
            epochs[item_id] = epochs.get(item_id, 0) + 1
        step = execution.get("next_step") or {}
        action = (step.get("action")
                  or items.get(item_id, {}).get("next_action", "Inspect execution"))
        identity = {"item": item_id, "epoch": epochs.get(item_id, 0),
                    "next_step": step, "action": action,
                    "assignment": execution.get("assignment")}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
        for target in targets:
            target_digest = hashlib.sha256(target.encode()).hexdigest()[:8]
            key = f"packet-step-{digest}-{target_digest}"
            current.add(key)
            if key not in data["messages"]:
                message = {"member": target, "from": "packet-scheduler", "state": "queued",
                           "created_at": now, "execution": execution,
                           "text": f"Canonical packet {data['packet']} item {item_id} has an "
                                   f"executable step: {action}. Inspect work_packets execution "
                                   "and record the owner/next-action disposition. Readiness grants "
                                   "no authority; preserve existing claims and operation holds."}
                message["coordination_event"] = event(key, message, data["id"])
                message["coordination_event"]["kind"] = "packet_step"
                data["messages"][key] = message
    sync_due_reviews(data, items, now)
    data["packet_readiness"] = {"observed_at": now, "candidates": candidate_items,
                                "epochs": epochs}
    for key, message in data["messages"].items():
        entry = message.get("coordination_event")
        if (entry and entry["kind"] == "packet_step" and key not in current
                and entry["state"] != "dispositioned"):
            entry.update(state="dispositioned", actionable=False, disposition={
                "action": "superseded", "reason": "Fresh canonical execution changed eligibility",
                "owner": "packet-scheduler", "next_action": "Inspect current canonical execution",
                "evidence": [f"work_packets coordinator/next and execution observed at {now}"]})


def inbox_enable(team: str, consumer_version: int = typer.Option(...),
                 evidence: list[str] = typer.Option(...)) -> None:
    """Enroll only after the native v1 consumer is installed; retain its acceptance receipt."""
    if consumer_version != 1 or not evidence or not all(ref.strip() for ref in evidence):
        raise typer.BadParameter("Version 1 and verified native installation evidence are required")
    h = api()
    with h.locked(team) as data:
        bind_anchor(data)
        if not supervisors(data):
            raise typer.BadParameter("A registered supervisor thread is required before enrollment")
        binding = enrollment_binding(data)
        previous = data.get("inbox_consumer")
        if previous:
            if (previous.get("version"), previous.get("evidence"), previous.get("binding")) != (
                    1, evidence, binding):
                raise typer.BadParameter("Inbox enrollment has a different binding/evidence")
            h.save(team, data)  # Repair an interrupted projection without re-enrolling.
            typer.echo(json.dumps(previous))
            return
        data["inbox_consumer"] = {"version": 1, "evidence": evidence,
                                  "enrolled_at": time.time(), "binding": binding}
        for message in data["messages"].values():
            if message.get("delivery_hold") == "native_inbox_enrollment_required":
                message.pop("delivery_hold")
        h.save(team, data)
        typer.echo(json.dumps(data["inbox_consumer"]))


def admission_cursor(thread: str) -> dict:
    path = codex_home() / "supervisor-inbox-cursors" / f"{UUID(thread)}.json"
    directory = path.parent.stat()
    if (path.parent.is_symlink() or directory.st_mode & 0o777 != 0o700
            or directory.st_uid != os.getuid()):
        raise typer.BadParameter("Native cursor directory must be private and not a symlink")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_size > 4096):
            raise typer.BadParameter("Native cursor requires private regular file <=4096 bytes")
        encoded = stream.read(4097)
    if len(encoded) > 4096:
        raise typer.BadParameter("Native cursor exceeds its byte limit")
    cursor = json.loads(encoded)
    generation = cursor.get("generation")
    if (not isinstance(generation, int) or isinstance(generation, bool)
            or not 0 <= generation <= 2**64-1 or not isinstance(cursor.get("suspended"), bool)):
        raise typer.BadParameter("Invalid native cursor generation/suspension state")
    if not isinstance(cursor.get("in_flight", False), bool):
        raise typer.BadParameter("Invalid native admission state")
    for key in ("reservation", "admission_token", "last_resolution"):
        token = cursor.get(key)
        if token is not None and (not isinstance(token, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,160}", token)):
            raise typer.BadParameter("Invalid native admission token")
    return cursor


def apply_admission_receipts(data: dict) -> int:
    """Apply immutable native turn receipts under the team lock.

    Entries stay actionable until dispositioned. Receipt files are retained for audit;
    missing, malformed, or unbound files never change event state.
    """
    changed = 0
    for member, thread in supervisors(data).items():
        root = codex_home() / "supervisor-delivery-receipts"
        directory = root / str(UUID(thread))
        try:
            for path in (root, directory):
                info = path.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o700):
                    raise ValueError("Invalid receipt directory")
            paths = sorted(directory.glob("*.json"))
        except (OSError, ValueError):
            continue
        for path in paths:
            try:
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}\.json", path.name):
                    continue
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as stream:
                    metadata = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                            or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_size > 4096):
                        continue
                    receipt = json.loads(stream.read(4097))
                token = receipt.get("admission_token")
                turn = receipt.get("turn_id")
                ids = receipt.get("event_ids")
                if (receipt.get("version") != 1 or receipt.get("thread_id") != str(UUID(thread))
                        or token != path.stem or not isinstance(turn, str) or not turn
                        or not isinstance(ids, list) or len(ids) > 3
                        or not all(isinstance(i, str) and 0 < len(i) <= 120 for i in ids)):
                    continue
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            for event_id in ids:
                message = data["messages"].get(event_id, {})
                entry = message.get("coordination_event")
                if (message.get("member") != member or not entry
                        or entry.get("state") != "persisted"):
                    continue
                entry.update(state="delivered", delivered_at=time.time(),
                             delivery={"state": "delivered", "turn_id": turn,
                                       "admission_token": token})
                changed += 1
    return changed


def admission_reconcile(team: str, member: str = typer.Option(...),
                        reservation: str = typer.Option(...), decision: str = typer.Option(...),
                        evidence: list[str] = typer.Option(...),
                        authority: str = typer.Option(...)) -> None:
    """Operator-only admission recovery; references do not prove approval or retry rights."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", reservation):
        raise typer.BadParameter("Invalid admission reservation token")
    if decision not in {"not_admitted", "completed", "interrupted"}:
        raise typer.BadParameter("Decision must be not_admitted, completed or interrupted")
    if (not evidence or len(evidence) > 32 or not all(
            isinstance(ref, str) and ref.strip() and len(ref) <= 4096 for ref in evidence)
            or not authority.strip() or len(authority) > 4096):
        raise typer.BadParameter("Bounded nonempty evidence and operator authority refs required")
    data = api().load(team)
    thread = supervisors(data).get(member)
    if not thread:
        raise typer.BadParameter("A registered supervisor recipient is required")
    thread = str(UUID(thread))
    root = codex_home() / "supervisor-admission-resolutions"
    directory = root / thread
    path = directory / f"{reservation}.json"
    lock_path = path.with_suffix(".lock")
    if any(value.is_symlink() for value in (root, directory, path, lock_path)):
        raise typer.BadParameter("Admission resolution paths must not be symlinks")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    os.chmod(directory, 0o700)
    record = {"version": 1, "thread_id": thread, "reservation": reservation,
              "decision": decision, "evidence": evidence, "authority": authority}
    with lock_path.open("a") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            metadata = path.stat()
            if (metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_size > 8192 or not stat.S_ISREG(metadata.st_mode)):
                raise typer.BadParameter(
                    "Existing resolution requires an owner-private file <=8192 bytes")
            previous = json.loads(path.read_text())
            if {key: value for key, value in previous.items() if key != "created_at"} != record:
                raise typer.BadParameter("Admission resolution has different immutable data")
            typer.echo(json.dumps(previous))
            return
        cursor = admission_cursor(thread)
        token = cursor.get("reservation") or cursor.get("admission_token")
        if (reservation != token or not (cursor.get("reservation") or cursor.get("in_flight"))
                or cursor.get("last_resolution") == reservation):
            raise typer.BadParameter("Reservation does not match current unresolved admission")
        record["created_at"] = time.time()
        if len(json.dumps(record, indent=2).encode()) > 8192:
            raise typer.BadParameter("Admission resolution exceeds its 8192-byte limit")
        atomic_write(path, record)
        typer.echo(json.dumps(record))


def register_commands(app: typer.Typer) -> None:
    app.command("admission-reconcile")(admission_reconcile)
    app.command("inbox-enable")(inbox_enable)
    app.command("inbox")(inbox)
    app.command("inbox-ack")(inbox_ack)
    app.command("inbox-resume")(inbox_resume)
    app.command("disposition")(disposition)
