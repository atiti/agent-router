"""Reconcile existing waits and owner-bound work on a bounded local schedule."""

from __future__ import annotations

import fcntl
import io
import json
import os
import subprocess
import time
from collections import Counter, defaultdict
from contextlib import contextmanager, redirect_stdout
from datetime import datetime
from pathlib import Path

import typer

from . import dashboard_actions as actions
from . import dashboard_decisions as decisions
from . import dashboard_packet_routes as routes
from . import herdr_lifecycle, herdr_team, waits
from .config import codex_home
from .dashboard import _rows, packets_root
from .shared_server import state_dir
from .supervisor_inbox import atomic_write, supervisors

MAX_MONITOR_STARTS = 1
QUIET_SECONDS = 30 * 60
TERMINAL_STATES = {"done", "cancelled"}
WAIT_STATES = {"waiting_worker", "waiting_external", "coordination_required"}


def status_path() -> Path:
    return state_dir() / "maintenance" / "status.json"


@contextmanager
def exclusive(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def held(path: Path) -> bool:
    try:
        with path.open("r") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except FileNotFoundError:
        pass
    return False


def stamp(text: str | None) -> float | None:
    try:
        parsed = datetime.fromisoformat((text or "").replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, OverflowError, TypeError, AttributeError):
        return None


def read_teams() -> list[dict]:
    teams = []
    for path in sorted((state_dir() / "herdr-teams").glob("*/team.json")):
        try:
            team = routes.read_record(path)
            if team.get("id") == path.parent.name:
                teams.append(team)
        except (OSError, ValueError):
            continue
    return teams


def live_supervisors(team: dict) -> int:
    count = 0
    for member in supervisors(team):
        record = (team.get("members") or {}).get(member)
        if record:
            terminal = herdr_team.own_terminal(team, record)
            if terminal and terminal.get("state") == "live":
                count += 1
        elif team.get("anchor_terminal"):
            if any(pane.get("terminal_id") == team["anchor_terminal"]
                   for pane in herdr_team.tui.panes(team["tui_session"])):
                count += 1
    return count


def reconcile_monitors(teams: list[dict], apply: bool) -> list[dict]:
    groups = defaultdict(list)
    for team in teams:
        groups[(team.get("namespace"), team.get("packet"))].append(team)
    result = []
    starts = 0
    for names, bound in groups.items():
        try:
            running = [team for team in bound
                       if held(herdr_team.path_for(team["id"]).with_suffix(".monitor.lock"))]
            if running:
                for team in running:
                    seen = (team.get("monitor") or {}).get("checked_at")
                    fresh = isinstance(seen, (int, float)) and 0 <= time.time() - seen <= 120
                    result.append({"team": team["id"],
                                   "state": "running" if fresh else "heartbeat_overdue"})
                continue
            live = [(team, live_supervisors(team)) for team in bound
                    if (team.get("inbox_consumer") or {}).get("version") == 1]
            total = sum(count for _, count in live)
            if total != 1:
                result.append({"packet": "/".join(str(i) for i in names),
                               "state": "ambiguous" if total else "no_live_enrolled_supervisor"})
                continue
            team = next(team["id"] for team, count in live if count == 1)
            if apply and starts < MAX_MONITOR_STARTS:
                with redirect_stdout(io.StringIO()):
                    herdr_lifecycle.start_monitor(team)
                starts += 1
                result.append({"team": team, "state": "start_requested"})
            else:
                result.append({"team": team, "state": "eligible"})
        except (OSError, ValueError, RuntimeError, typer.BadParameter, subprocess.SubprocessError):
            result.append({"packet": "/".join(str(i) for i in names), "state": "inspection_failed"})
    return result


def classify(item: dict, now: float) -> tuple[str, str | None]:
    step = item.get("next_step") or {}
    state = step.get("state")
    outcome = item.get("outcome_wait") or {}
    if state == "awaiting_user" or (
        outcome.get("state") == "awaiting_user" and state not in {"ready", "active"}
    ):
        return "awaiting_user", None
    if any(op.get("state") in {"prepared", "unknown"}
           for op in (item.get("operations") or {}).values()):
        return "unresolved_operation", "verifier"
    if (item.get("goal_review") or {}).get("advances_goal") is False:
        return "requires_replan", "reviewer"
    raw_date = item.get("next_review")
    review = stamp(raw_date)
    if raw_date is not None and review is None:
        return "invalid_review_date", "verifier"
    if review is not None and review > now:
        return "scheduled", None
    if state in WAIT_STATES or (outcome.get("state") in WAIT_STATES
                                and state not in {"ready", "active"}):
        return ("review_due", "verifier") if review is not None else ("wait_without_review", None)
    if item.get("status") == "blocked" and state not in {"ready", "active"}:
        return "blocker_needs_owner", "reviewer"
    if not item.get("assignment"):
        return "no_assignment", None
    if state not in {"ready", "active"} or item.get("status") not in {"active", "blocked"}:
        return "no_executable_step", None
    since = stamp((item.get("checkpoint") or {}).get("at")
                  or (item.get("assignment") or {}).get("at"))
    if review is None and (since is None or now - since < QUIET_SECONDS):
        return "recent_or_unverified", None
    return "work_due", None


def packet_rows():
    root = packets_root() / "tasks" / "packets"
    for path in sorted(root.glob("*/*.json")):
        try:
            packet = json.loads(path.read_text())
            namespace, packet_id = packet.get("namespace"), packet.get("id")
            if (namespace, packet_id) != (path.parent.name, path.stem):
                continue
            for item in packet.get("items") or []:
                if item.get("status") not in TERMINAL_STATES:
                    yield (namespace, packet_id, item["id"]), packet, item
        except (OSError, ValueError, KeyError, TypeError):
            continue


def reconcile_packets(teams: list[dict], now: float, monitors: list[dict] | None = None) -> dict:
    monitored = {row.get("team") for row in (monitors or []) if row["state"] == "running"}
    supervised_packets = {
        (team["namespace"], team["packet"])
        for team in teams
        if team["id"] in monitored and (team.get("inbox_consumer") or {}).get("version") == 1
        and (team.get("monitor") or {}).get("live_review_recipients")
    }
    managed = {record.get("thread") for team in teams
               for record in (team.get("members") or {}).values()}
    managed.update(thread for team in teams for thread in supervisors(team).values())
    pending = {w["thread_id"] for w in waits.load() if w["state"] in {"pending", "fired"}}
    # Only known, non-archived local conversations are candidates. An owner label
    # can resolve through a verified binding, but never through a review fallback.
    local = {row["id"] for row in _rows(codex_home() / "state_5.sqlite",
                                      "SELECT id FROM threads WHERE archived = 0")}
    counts = Counter()
    details = []
    for key, packet, item in packet_rows():
        reason, role = classify(item, now)
        owner = (item.get("assignment") or {}).get("session")
        step = item.get("next_step") or {}
        outcome = item.get("outcome_wait") or {}
        required = {slot: wait for slot, wait in (("step", step.get("wait") or {}),
                                                   ("outcome", outcome))
                    if (step.get("state") if slot == "step" else outcome.get("state"))
                    == "awaiting_user"}
        feedback = {slot: {"owner": wait.get("owner"), "question_ref": wait.get("question_ref"),
                           "resume_when": wait.get("resume_when"), "state": "answer_needed"}
                    for slot, wait in required.items()}
        if feedback:
            try:
                for receipt in decisions.recorded(key):
                    slot = receipt.get("slot")
                    if slot not in feedback or feedback[slot]["state"] == "decision_delivered":
                        continue
                    if (receipt.get("revision") != item.get("revision")
                            or not receipt.get("turn_id")):
                        continue
                    try:
                        verified = decisions.verify_receipt(key, receipt["id"], None)
                    except actions.ActionError:
                        continue
                    feedback[slot].update(state="decision_delivered", decision_id=verified["id"])
                if reason == "awaiting_user" and all(
                    entry["state"] == "decision_delivered" for entry in feedback.values()
                ):
                    reason = "decision_delivered"
            except (OSError, ValueError, KeyError, TypeError):
                reason = "decision_receipt_unreadable"

        thread = None
        binding = "unassigned"
        if owner:
            try:
                route = routes.route(key, item)
                thread = route["thread"] if route["kind"] == "owner" else None
                binding = "verified" if thread and (thread in local or thread in managed) else (
                    "unverified"
                )
            except routes.HERDR_ERRORS:
                binding = "check_failed"
        monitored_packet = key[:2] in supervised_packets
        if reason in {"work_due", "review_due"}:
            if reason == "work_due" and (
                set(item.get("dependencies", [])) -
                {x["id"] for x in packet["items"] if x.get("status") == "done"}
            ):
                reason = "dependencies_pending"
            elif monitored_packet:
                reason = "supervisor_followup"
            elif binding == "check_failed":
                reason = "owner_check_failed"
            elif binding != "verified":
                reason = "no_verified_conversation"
            elif thread in pending:
                reason = "already_has_wait"
            else:
                # Plain conversations need atomic idle-only admission before automatic wake.
                reason = "needs_idle_admission"
        counts[reason] += 1
        details.append({"packet": "/".join(key), "state": reason, "thread": thread,
                        "revision": item.get("revision"), "owner": owner, "binding": binding,
                        "monitored": monitored_packet, "suggested_role": role,
                        "feedback": feedback})
    return {"counts": dict(counts), "items": details}


def run_once(apply: bool = False) -> dict:
    now = time.time()
    with exclusive(status_path().with_suffix(".lock")) as acquired:
        if not acquired:
            return {"state": "already_running"}
        teams = read_teams()
        monitors = reconcile_monitors(teams, apply)
        report = {"checked_at": now, "apply": apply, "monitors": monitors,
                  "packets": reconcile_packets(teams, now, monitors)}
        pending = sum(w["state"] in {"pending", "fired"} for w in waits.load())
        started = waits.ensure_daemon() if apply and pending else False
        report["waits"] = {"pending_or_fired": pending, "start_requested": started,
                           "running": waits.daemon_running()}
        if apply:
            atomic_write(status_path(), report)
        return report

