"""Durable worker health, message receipt and conservative completion cleanup."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime

import typer


def api():
    from . import herdr_team
    return herdr_team


def recipient_identity(data: dict) -> str:
    h = api()
    thread = os.environ.get("CODEX_THREAD_ID")
    member = os.environ.get("AGENTROUTE_HERDR_MEMBER")
    if (os.environ.get("AGENTROUTE_HERDR_TEAM") == data["id"]
            and member in data["members"] and thread
            and data["members"][member].get("thread") == thread):
        return member
    if thread and data.get("anchor_terminal"):
        try:
            record = json.loads(h.tui.registry_path(data["tui_session"], thread).read_text())
            if record["terminal_id"] == data["anchor_terminal"]:
                return "supervisor"
        except (OSError, ValueError, KeyError):
            pass
    raise typer.BadParameter("Acknowledge from the actual recipient's Codex session.")


def ack(team: str, message_id: str) -> None:
    """Record receipt by the bound recipient; does not approve or complete work."""
    h = api()
    with h.locked(team) as data:
        recipient = recipient_identity(data)
        message = data["messages"][message_id]
        if message["member"] != recipient:
            raise typer.BadParameter("Only the addressed recipient may acknowledge this message")
        if message.get("target_thread") not in (None, os.environ.get("CODEX_THREAD_ID")):
            raise typer.BadParameter("Message belongs to another recipient session")
        allowed = {"submitted", "unknown", "prepared"}
        if message.get("coordination_event"):
            allowed |= {"queued", "rejected"}
        if message["state"] not in allowed:
            raise typer.BadParameter("Unsupported delivery state")
        if message["state"] != "submitted" and not message.get("coordination_event"):
            message["delivery_reconciliation"] = "Confirmed received by the bound recipient"
            message["state"] = "submitted"
        message.setdefault("acknowledgment", {"at": time.time(), "by": recipient,
                                              "thread": os.environ.get("CODEX_THREAD_ID")})
        if message.get("coordination_event"):
            entry = message["coordination_event"]
            if entry["state"] != "dispositioned":
                entry.update(state="acked", acknowledged_at=time.time())
        h.save(team, data)
        typer.echo(json.dumps(message["acknowledgment"]))


def completed(item: dict | None) -> bool:
    if not item or item.get("status") != "done" or item.get("assignment"):
        return False
    gates = item.get("gates", {})
    names = ("implementation", "checks", "merge", "deployment", "acceptance", "handoff", "cleanup")
    return (all(gates.get(k, {}).get("status") in {"passed", "not_applicable"}
                for k in names if gates.get(k) is not None)
            and all(gates.get(k) is not None for k in names)
            and all(c.get("evidence") for c in item.get("criteria", []))
            and not any(o.get("state") in {"prepared", "unknown"}
                        for o in item.get("operations", {}).values()))


def cleanup(team: str, member: str, apply: bool = False) -> None:
    """Archive only verified-complete, exited workers; --apply closes their owned viewer."""
    h = api()
    with h.locked(team) as data:
        record = data["members"][member]
        if record.get("cleanup", {}).get("state") == "archived":
            return
        binding = record.get("packet_binding", {})
        packet = json.loads(h.packet_command(data, "status", "--packet", data["packet"]))
        item = next((i for i in packet["items"] if i["id"] == binding.get("item")), None)
        if not completed(item):
            raise typer.BadParameter("Canonical completion and all delivery gates are required")
        terminal = h.own_terminal(data, record)
        if not terminal or terminal["state"] != "finished":
            raise typer.BadParameter("Worker must exit at a safe boundary before cleanup")
        if not apply:
            typer.echo(json.dumps({"member": member, "eligible": True, "thread": record["thread"]}))
            return
        # Preserve durable history and intent before closing an exact viewer identity.
        record["cleanup"] = {"state": "prepared", "at": time.time(),
                             "item_revision": item["revision"], "address": terminal["address"]}
        h.save(team, data)
        view = record.get("view", {})
        pane = next((p for p in h.tui.panes(data["tui_session"])
                     if p["terminal_id"] == view.get("terminal_id")), None)
        if pane:
            info = h.tui.command(data["tui_session"], "pane", "process-info", "--pane",
                                 pane["pane_id"])["process_info"]
            processes = info.get("foreground_processes", [])
            if not view.get("pid") or not any(p["pid"] == view["pid"] for p in processes):
                raise typer.BadParameter("Viewer ownership changed; retain pane for reconciliation")
            h.tui.command(data["tui_session"], "pane", "close", pane["pane_id"])
        record["cleanup"]["state"] = "archived"
        record["cleanup"]["verified_at"] = time.time()
        h.save(team, data)
        # Core terminal history, Codex conversation and message receipts are retained.


def step_handoff_item(h, data: dict, record: dict, step: str, revision: int) -> dict:
    binding = record.get("packet_binding", {})
    packet = json.loads(h.packet_command(data, "status", "--packet", data["packet"]))
    item = next((i for i in packet["items"] if i["id"] == binding.get("item")), None)
    if (record.get("spec", {}).get("role") != "worker" or not binding.get("session")
            or not record.get("thread") or not item or item.get("revision") != revision
            or item.get("status") not in {"blocked", "ready"}
            or item.get("assignment") is not None
            or (item.get("next_step") or {}).get("id") != step
            or item["next_step"].get("state") != "done"
            or (item.get("last_release") or {}).get("session") != binding["session"]
            or any(op.get("state") in {"prepared", "unknown"}
                   for op in item.get("operations", {}).values())):
        raise typer.BadParameter("Done step and exact released owner/revision required; "
                                 "active owners or uncertain effects cannot retire")
    return item


def retire_step(team: str, member: str, step: str = typer.Option(...),
                expected_revision: int = typer.Option(...),
                evidence: list[str] = typer.Option(...), apply: bool = False,
                reconcile_closed: bool = False, intent_fingerprint: str | None = None) -> None:
    """Retire a released done-step worker, preserving the incomplete canonical outcome."""
    if not evidence or not all(isinstance(ref, str) and ref.strip() for ref in evidence):
        raise typer.BadParameter("Explicit handoff/inspection evidence is required")
    h = api()
    with h.locked(team) as data:
        record = data["members"][member]
        terminal = h.own_terminal(data, record)
        if not terminal or terminal.get("state") != "finished":
            raise typer.BadParameter("The exact worker terminal must exist and be finished")
        view = {key: record.get("view", {}).get(key) for key in ("pane_id", "terminal_id", "pid")}
        identity = {"team": team, "member": member, "attempt": record.get("attempt"),
                    "thread": record.get("thread"), "binding": record.get("packet_binding"),
                    "terminal": terminal["address"], "view": view,
                    "step": step, "item_revision": expected_revision}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        previous = record.get("cleanup")
        if previous:
            if (previous.get("kind") != "step_handoff"
                    or previous.get("intent_fingerprint") != fingerprint
                    or previous.get("identity") != identity):
                raise typer.BadParameter(
                    "Retirement identity changed: kind/step/revision/attempt/view")
            if intent_fingerprint not in (None, fingerprint):
                raise typer.BadParameter("Intent fingerprint mismatch")
            if previous["state"] == "archived":
                typer.echo(json.dumps(previous))
                return
            if not reconcile_closed:
                raise typer.BadParameter("Retirement outcome unresolved; inspect then use "
                                         "--reconcile-closed with the exact intent fingerprint")
        elif reconcile_closed:
            raise typer.BadParameter("No original retirement intent to reconcile")
        item = step_handoff_item(h, data, record, step, expected_revision)
        if previous and any(previous.get(saved) != item.get(current) for saved, current in (
                ("step", "next_step"), ("last_release", "last_release"),
                ("outcome_wait", "outcome_wait"), ("outcome_status", "status"))):
            raise typer.BadParameter("Original canonical handoff snapshot changed")
        panes = h.tui.panes(data["tui_session"])
        pane = next((p for p in panes if view["terminal_id"]
                     and p["terminal_id"] == view["terminal_id"]), None)
        if reconcile_closed:
            if intent_fingerprint != fingerprint:
                raise typer.BadParameter("Explicit original intent fingerprint is required")
            if pane or any(p.get("pane_id") == view["pane_id"] for p in panes if view["pane_id"]):
                raise typer.BadParameter("Original viewer is still present or its pane was reused")
        elif pane:
            if pane.get("pane_id") != view["pane_id"] or not view["pid"]:
                raise typer.BadParameter("Viewer ownership changed; retain it for reconciliation")
            info = h.tui.command(data["tui_session"], "pane", "process-info", "--pane",
                                 pane["pane_id"])["process_info"]
            if not any(process["pid"] == view["pid"]
                       for process in info.get("foreground_processes", [])):
                raise typer.BadParameter("Viewer ownership changed; retain it for reconciliation")
        elif any(p.get("pane_id") == view["pane_id"] for p in panes if view["pane_id"]):
            raise typer.BadParameter("Original viewer pane was reused")
        if not apply:
            typer.echo(json.dumps({"eligible": True, "kind": "step_handoff",
                                   "outcome_complete": False, "identity": identity,
                                   "intent_fingerprint": fingerprint, "step": item["next_step"],
                                   "last_release": item["last_release"],
                                   "outcome_wait": item.get("outcome_wait"),
                                   "reconcile_closed": reconcile_closed}))
            return
        # Fresh canonical read before the sole external effect; never mutate that item.
        fresh = step_handoff_item(h, data, record, step, expected_revision)
        if fresh != item:
            raise typer.BadParameter("Canonical handoff changed during inspection")
        if not previous:
            record["cleanup"] = {"state": "prepared", "kind": "step_handoff",
                "outcome_complete": False, "at": time.time(), "identity": identity,
                "intent_fingerprint": fingerprint, "item_revision": item["revision"],
                "step": item["next_step"], "last_release": item["last_release"],
                "outcome_status": item["status"], "outcome_wait": item.get("outcome_wait"),
                "evidence": evidence}
            h.save(team, data)
        try:
            if pane and not reconcile_closed:
                h.tui.command(data["tui_session"], "pane", "close", pane["pane_id"])
            remaining = h.tui.panes(data["tui_session"])
            if any((view["terminal_id"] and p.get("terminal_id") == view["terminal_id"])
                   or (view["pane_id"] and p.get("pane_id") == view["pane_id"])
                   for p in remaining):
                raise RuntimeError("Viewer close not independently verified; target remains/reused")
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            record["cleanup"].update(state="unknown", error=str(error))
            h.save(team, data)
            raise
        if reconcile_closed:
            record["cleanup"]["reconciliation"] = {"at": time.time(), "evidence": evidence,
                "intent_fingerprint": fingerprint, "observed": "exact viewer/pane absent"}
        record["cleanup"].update(state="archived", verified_at=time.time())
        h.save(team, data)
        typer.echo(json.dumps(record["cleanup"]))


def poll(team: str, stale_after: int, ack_after: int, now: float | None = None) -> dict:
    h = api()
    now = time.time() if now is None else now
    alerts = []
    with h.locked(team) as data:
        packet = json.loads(h.packet_command(data, "status", "--packet", data["packet"]))
        items = {i["id"]: i for i in packet["items"]}
        for name, member in data["members"].items():
            if member.get("cleanup", {}).get("state") == "archived":
                continue
            terminal = h.own_terminal(data, member)
            state = h.reported_state(terminal)
            member["terminal_observation"] = {"at": now,
                "terminal_state": terminal.get("state") if terminal else "missing",
                "reported_state": state}
            health = member.setdefault("health", {"last_activity": now, "state_since": now})
            binding = member.get("packet_binding", {})
            item = items.get(binding.get("item"), {})
            # Output activity and checkpoint activity are tracked independently.
            screen = (h.core(data, "read", terminal["address"], "--json").get("text", "")
                      if terminal and terminal.get("state") == "live" else "")
            fingerprint = hashlib.sha256(json.dumps([
                screen, terminal.get("turn") if terminal else None,
                terminal.get("status_ms") if terminal else None,
                item.get("checkpoint", {}).get("at")], sort_keys=True).encode()).hexdigest()
            if health.get("fingerprint") != fingerprint:
                health.update(fingerprint=fingerprint, last_activity=now)
            if health.get("state") != state:
                health.update(state=state, state_since=now)
            checkpoint = item.get("checkpoint", {}).get("at")
            health.update(checked_at=now, checkpoint_at=checkpoint)
            if completed(item):
                alerts.append(f"{name}: packet complete; exit safely then cleanup {team} {name}")
            elif not terminal or terminal["state"] != "live":
                alerts.append(f"{name}: terminal missing/exited; reconcile before resume")
            elif state in {"blocked", "error"}:
                alerts.append(f"{name}: {state}; inspect and follow approval boundaries")
            elif now - health["last_activity"] >= stale_after:
                alerts.append(f"{name}: observed activity overdue")
            elif state in {"idle", "done"} and now-health["state_since"] >= ack_after:
                alerts.append(f"{name}: waiting for follow-up; packet is not complete")
            if checkpoint and not completed(item):
                age = now-datetime.fromisoformat(checkpoint.replace("Z", "+00:00")).timestamp()
                if age >= stale_after:
                    alerts.append(f"{name}: canonical checkpoint overdue")
        for key, message in data["messages"].items():
            if key.startswith("health-"):
                continue  # Avoid recursive health-alert acknowledgment debt.
            if message["state"] in {"prepared", "unknown", "rejected"}:
                alerts.append(f"message {key}: delivery {message['state']}; inspect, do not resend")
                continue
            if not message.get("created_at"):
                continue  # Legacy submissions predate acknowledgments; do not invent receipt debt.
            since = message.setdefault("tracking_since", message.get("submitted_at", now))
            if (message["state"] == "submitted" and not message.get("acknowledgment")
                    and now-since >= ack_after):
                alerts.append(f"message {key} to {message['member']}: receipt unacknowledged")
        from .supervisor_inbox import sync_packet_steps

        sync_packet_steps(data, items, now)
        monitor = data.setdefault("monitor", {})
        if data.get("anchor_terminal"):
            pane = next((p for p in h.tui.panes(data["tui_session"])
                         if p["terminal_id"] == data["anchor_terminal"]), None)
            supervisor_state = pane.get("agent_status", "unknown") if pane else "missing"
            monitor["supervisor_state"] = supervisor_state
            monitor.pop("notification_hold", None)
            if supervisor_state in {"blocked", "missing"}:
                monitor["notification_hold"] = (
                    f"Supervisor {supervisor_state}; inspection continues")
                alerts.append(monitor["notification_hold"])
        monitor.update(checked_at=now, alerts=alerts)
        h.save(team, data)
    return {"team": team, "checked_at": now, "alerts": alerts}


def monitor(team: str, watch: bool = False, notify: bool = False,
            stale_after: int = typer.Option(900, min=60),
            ack_after: int = typer.Option(300, min=30),
            interval: int = typer.Option(30, min=5)) -> None:
    """Track activity and receipt deadlines; notify without restarting work."""
    import fcntl

    h = api()
    lock_path = h.path_for(team).with_suffix(".monitor.lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise typer.BadParameter("Monitor already running") from error
        while True:
            try:
                result = poll(team, stale_after, ack_after)
                typer.echo(json.dumps(result))
                data = h.load(team)
                digest = hashlib.sha256(json.dumps(result["alerts"]).encode()).hexdigest()[:16]
                previous = data.get("monitor", {}).get("notification", {})
                if notify:
                    with h.locked(team) as current:
                        monitor_state = current.setdefault("monitor", {})
                        if monitor_state.get("event_digest") != digest:
                            sequence = monitor_state.get("event_sequence", 0) + 1
                            monitor_state.update(event_digest=digest, event_sequence=sequence,
                                                 event_key=f"health-{digest}-{sequence}")
                        key = monitor_state["event_key"]
                        h.save(team, current)  # Stable occurrence identity precedes delivery.
                    if (result["alerts"] and key not in data["messages"]
                            and previous.get("event_key") != key):
                        due = not previous.get("at") or time.time()-previous["at"] >= stale_after
                        prompt = due and not data.get("monitor", {}).get("notification_hold")
                        h.send(team, "supervisor", key,
                               "Team health requires inspection (no automatic approval/restart):\n"
                               + "\n".join(result["alerts"]), notify=bool(prompt))
                        with h.locked(team) as current:
                            current.setdefault("monitor", {})["notification"] = {
                                "digest": digest, "event_key": key,
                                "at": time.time() if prompt else previous.get("at")}
                            h.save(team, current)
                    # Fresh observations can supersede older health sets only after the
                    # new pending event is durable, avoiding an empty-inbox completion gap.
                    with h.locked(team) as current:
                        for message_key, message in current["messages"].items():
                            entry = message.get("coordination_event")
                            if (entry and entry["kind"] == "health"
                                    and entry["state"] != "dispositioned" and message_key != key):
                                entry.update(state="dispositioned", actionable=False,
                                    disposition={"action": "superseded" if result["alerts"]
                                                 else "resolved",
                                        "reason": "Fresh health poll replaced this set",
                                        "owner": "team-monitor",
                                        "next_action": "Inspect the latest health event" if
                                            result["alerts"] else "No current health alert",
                                        "evidence": [f"herdr poll {result.get('checked_at')} "
                                                     f"alerts-sha256:{digest}"]})
                        h.save(team, current)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError,
                    typer.BadParameter) as error:
                typer.echo(f"Monitor inspection failed: {error}", err=True)
                if not watch:
                    raise
            if not watch:
                return
            time.sleep(interval)


def start_monitor(team: str) -> None:
    """Start one detached per-team monitor, retaining a local log."""
    h = api()
    h.load(team)
    import fcntl

    path = h.path_for(team).with_suffix(".monitor.lock")
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            typer.echo("Monitor already running")
            return
    log = path.with_suffix(".log")
    with log.open("ab") as stream:
        process = subprocess.Popen([sys.executable, "-m", "agentroute.cli", "herdr", "team",
                                    "monitor", team, "--watch", "--notify"],
                                   stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                                   start_new_session=True)
    with h.locked(team) as data:
        data.setdefault("monitor", {}).update(pid=process.pid, log=str(log))
        h.save(team, data)
    typer.echo(str(process.pid))


def register_commands(app: typer.Typer) -> None:
    app.command("ack")(ack)
    app.command("monitor")(monitor)
    app.command("monitor-start")(start_monitor)
    app.command("cleanup")(cleanup)
    app.command("retire-step")(retire_step)
