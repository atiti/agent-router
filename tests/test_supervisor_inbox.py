import json
from unittest.mock import Mock

import pytest
import typer

from agentroute import herdr_team as h
from agentroute import supervisor_inbox as inbox
from agentroute.config import default_config
from agentroute.models import ReasonCode, RouteContext, Tier
from agentroute.router import Router

THREAD = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def team(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(inbox, "codex_home", lambda: tmp_path / "codex")
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    data = {"id": "demo", "namespace": "markster", "packet": "packet", "item": "item",
            "core_session": "ar123", "tui_session": "agentroute", "brief": "Deliver safely",
            "inbox_consumer": {"version": 1, "evidence": ["fixture-consumer"]},
            "members": {"boss": {"thread": THREAD, "attempt": "a", "launch": "started",
                                 "spec": {"role": "supervisor", "task": "Resolve owner overlaps"}},
                        "worker": {"thread": OTHER, "attempt": "b", "launch": "started",
                                   "spec": {"role": "worker"}}}, "messages": {}}
    with h.locked("demo"):
        h.save("demo", data)
    return data


def snapshot():
    return json.loads(inbox.path_for(THREAD).read_text())


def test_offline_supervisor_event_persists_without_pane_input(team, monkeypatch):
    monkeypatch.setattr(h, "own_terminal", lambda *_: None)
    core = Mock()
    monkeypatch.setattr(h, "core", core)
    h.send("demo", "boss", "owner-overlap", "Need exact file handover")
    value = snapshot()
    assert value["role"] == "supervisor"
    assert value["thread_id"] == THREAD
    assert value["live_children"][0]["state"] == "unknown"
    assert value["live_children"][0]["reconcile_required"]
    assert value["events"][0]["state"] == "persisted"
    assert value["events"][0]["actionable"]
    assert h.load("demo")["messages"]["owner-overlap"]["state"] == "queued"
    core.assert_not_called()
    assert inbox.path_for(THREAD).stat().st_mode & 0o777 == 0o600


def test_supervisor_never_prompts_and_legacy_unknown_never_retries(team, monkeypatch):
    lookup, core = Mock(), Mock()
    monkeypatch.setattr(h, "own_terminal", lookup)
    monkeypatch.setattr(h, "core", core)
    h.send("demo", "boss", "overlap", "owner overlap")
    assert snapshot()["events"][0]["delivery"]["state"] == "pending"
    with h.locked("demo") as data:
        data["messages"]["overlap"]["state"] = "unknown"
        data["messages"]["overlap"]["coordination_event"]["delivery"]["state"] = "unknown"
        h.save("demo", data)
    h.send("demo", "boss", "overlap", "owner overlap")
    core.assert_not_called()
    lookup.assert_not_called()
    assert len(snapshot()["events"]) == 1
    assert snapshot()["events"][0]["delivery"]["state"] == "unknown"
    with pytest.raises(typer.BadParameter, match="different content"):
        h.send("demo", "boss", "overlap", "new content")


def test_consume_ack_and_disposition_are_distinct_and_bound(team, monkeypatch):
    monkeypatch.setattr(h, "own_terminal", lambda *_: None)
    h.send("demo", "boss", "overlap", "owner overlap")
    with monkeypatch.context() as other:
        other.setenv("CODEX_THREAD_ID", OTHER)
        with pytest.raises(typer.BadParameter, match="bound supervisor"):
            inbox.inbox("demo", "boss")
    inbox.inbox("demo", "boss")
    assert snapshot()["events"][0]["state"] == "delivered"
    inbox.inbox_ack("demo", "overlap", "boss")
    assert snapshot()["events"][0]["state"] == "acked"
    assert snapshot()["events"][0]["actionable"]
    inbox.disposition("demo", "overlap", "owner_handoff", "Exact-file owner retained",
                      ["worker handover receipt"], "boss", None, None,
                      "existing-worker", "Existing worker finishes its claimed files")
    entry = h.load("demo")["messages"]["overlap"]["coordination_event"]
    assert not snapshot()["events"]
    assert entry["state"] == "dispositioned" and not entry["actionable"]
    assert entry["disposition"]["action"] == "owner_handoff"
    inbox.inbox_ack("demo", "overlap", "boss")
    assert not snapshot()["events"]


def test_wait_requires_delivered_question_and_resume_condition(team, monkeypatch):
    monkeypatch.setattr(h, "own_terminal", lambda *_: None)
    h.send("demo", "boss", "approval", "Need user approval")
    with pytest.raises(typer.BadParameter, match="delivered question"):
        inbox.disposition("demo", "approval", "awaiting_user", "Deployment approval",
                          ["reviewed plan"], "boss", None, None)
    inbox.disposition("demo", "approval", "awaiting_user", "Deployment approval",
                      ["reviewed plan"], "boss", "user-message-5", "User approves plan")
    assert snapshot()["events"][0]["wait"] == {
        "owner": "user", "delivery_state": "delivered", "question_ref": "user-message-5",
        "resume_condition": "User approves plan"}


def test_projection_repairs_interrupted_publish_and_noop_keeps_generation(team, monkeypatch):
    monkeypatch.setattr(h, "own_terminal", lambda *_: None)
    original = inbox.publish
    with monkeypatch.context() as fail:
        fail.setattr(inbox, "publish", Mock(side_effect=OSError("disk interrupted")))
        with pytest.raises(OSError):
            h.send("demo", "boss", "repair", "retained intent")
    assert "repair" in h.load("demo")["messages"]
    h.send("demo", "boss", "repair", "retained intent")
    before = snapshot()["generation"]
    with h.locked("demo") as data:
        original(data)
    assert snapshot()["generation"] == before
    assert len(snapshot()["events"]) == 1


def test_routing_ignores_forged_or_unbound_thread_role(team):
    assert inbox.routing_context(THREAD)["objective"] == "Resolve owner overlaps"
    data = h.load("demo")
    data["members"]["boss"]["spec"]["role"] = "worker"
    # Deliberately leave stale snapshot, as after a stopped owner/rebind.
    h.path_for("demo").write_text(json.dumps(data))
    assert inbox.routing_context(THREAD) is None
    assert inbox.routing_context(OTHER) is None
    assert inbox.routing_context("../escape") is None


def test_supervisor_floor_respects_explicit_route_and_policy():
    config = default_config()
    config.routing.switching.downgrade_confidence = 0
    context = RouteContext(session_id=THREAD, latest_prompt="list files",
                           current_tier=Tier.FAST, coordination_role="supervisor",
                           ongoing_objective="Deliver a cross-repository release")
    routed = Router(config).route(context)
    assert routed.tier >= Tier.SMART
    assert ReasonCode.SUPERVISOR_CAPABILITY_FLOOR in routed.reason_codes
    worker = Router(config).route(context.model_copy(update={"coordination_role": "worker"}))
    assert worker.tier < Tier.SMART
    manual = Router(config).route(context.model_copy(update={"latest_prompt": "@fast list files"}))
    assert manual.tier == Tier.FAST
    config.policy.max_tier = "normal"
    assert Router(config).route(context).tier == Tier.NORMAL


def test_wait_resume_keeps_receipt_and_cannot_rewrite_wait(team, monkeypatch):
    monkeypatch.setattr(h, "own_terminal", lambda *_: None)
    h.send("demo", "boss", "approval", "Need approval")
    args = ("demo", "approval", "awaiting_user", "Approval", ["plan"], "boss")
    inbox.disposition(*args, "question-1", "Exact approval")
    with pytest.raises(typer.BadParameter, match="already dispositioned"):
        inbox.disposition(*args, "question-2", "New condition")
    inbox.inbox_resume("demo", "approval", ["user-approved-plan-1"], "boss")
    entry = h.load("demo")["messages"]["approval"]["coordination_event"]
    assert len(snapshot()["events"]) == 1
    assert snapshot()["events"][0]["kind"] == "wait_resolved"
    assert snapshot()["events"][0]["resolves_event"] == "approval"
    assert snapshot()["events"][0]["actionable"]
    assert "wait" not in entry
    assert entry["wait_resolution"]["wait"]["question_ref"] == "question-1"
    assert entry["wait_resolution"]["evidence"] == ["user-approved-plan-1"]
    inbox.inbox_resume("demo", "approval", ["user-approved-plan-1"], "boss")
    assert len(snapshot()["events"]) == 1


def test_absent_thread_cannot_consume_unbound_recipient(team, monkeypatch):
    monkeypatch.delenv("CODEX_THREAD_ID")
    with pytest.raises(typer.BadParameter, match="bound supervisor"):
        inbox.recipient(team, "nonexistent")


def test_legacy_anchor_migrates_from_live_registry_before_ledger_save(team, monkeypatch, tmp_path):
    data = h.load("demo")
    del data["members"]["boss"]
    data["anchor_terminal"] = "native-supervisor"
    directory = tmp_path / "registry"
    directory.mkdir()
    import os
    (directory / f"{THREAD}.json").write_text(json.dumps({
        "terminal_id": "native-supervisor", "pid": os.getpid()}))
    monkeypatch.setattr(h.tui, "registry_path", lambda *_: directory / "unused.json")
    with h.locked("demo"):
        # The same native thread is now represented by an anchor; retire old test projection.
        inbox.path_for(THREAD).unlink()
        h.save("demo", data)
    assert h.load("demo")["anchor_thread"] == THREAD
    assert snapshot()["member"] == "supervisor"
    assert inbox.routing_context(THREAD)["role"] == "supervisor"


def test_informational_reports_do_not_create_coordination_debt(team, monkeypatch):
    monkeypatch.setattr(h, "own_terminal", lambda *_: None)
    h.send("demo", "boss", "info", "Progress report", actionable=False)
    assert not snapshot()["events"]
    assert h.load("demo")["messages"]["info"]["coordination_event"]["actionable"] is False
    h.send("demo", "boss", "info", "Progress report", actionable=False)
    with pytest.raises(typer.BadParameter, match="different content"):
        h.send("demo", "boss", "info", "Progress report", actionable=True)


def test_projection_compacts_history_and_exposes_pending_overflow(team):
    data = h.load("demo")
    for n in range(300):
        key = f"history-{n}"
        message = {"member": "boss", "from": "coordinator", "text": "x" * 50000,
                   "state": "queued"}
        entry = inbox.event(key, message, "demo")
        entry.update(state="dispositioned", actionable=False)
        message["coordination_event"] = entry
        data["messages"][key] = message
    with h.locked("demo"):
        h.save("demo", data)
    assert snapshot()["events"] == []
    assert snapshot()["overflow"]["events"] == 0
    for n in range(130):
        key = f"pending-{n}"
        message = {"member": "boss", "from": "coordinator", "text": "🪂" * 50000,
                   "state": "queued"}
        message["coordination_event"] = inbox.event(key, message, "demo")
        data["messages"][key] = message
    with h.locked("demo"):
        h.save("demo", data)
    value = snapshot()
    assert len(value["events"]) <= 128
    assert value["overflow"]["events"] == 130-len(value["events"])
    assert inbox.path_for(THREAD).stat().st_size <= 262144
    assert len(value["events"][0]["message"].encode()) <= 1024
    for entry in value["events"]:
        data["messages"][entry["id"]]["coordination_event"].update(
            state="dispositioned", actionable=False)
    with h.locked("demo"):
        h.save("demo", data)
    assert snapshot()["events"][0]["id"] == f"pending-{len(value['events'])}"
    assert len(data["messages"]) == 430


def test_observation_refresh_retains_generation_but_stale_child_needs_reconcile(team):
    import time
    data = h.load("demo")
    child = data["members"]["worker"]
    child["terminal_observation"] = {"at": time.time(), "terminal_state": "live",
                                     "reported_state": "working"}
    with h.locked("demo"):
        h.save("demo", data)
    value = snapshot()
    assert value["live_children"][0]["state"] == "running"
    assert value["live_children"][0]["source"] == "herdr-core"
    child["terminal_observation"]["at"] = time.time()
    with h.locked("demo"):
        h.save("demo", data)
    assert snapshot()["generation"] == value["generation"]
    child["terminal_observation"]["at"] -= 121
    with h.locked("demo"):
        h.save("demo", data)
    assert snapshot()["live_children"][0]["state"] == "unknown"
    assert snapshot()["live_children"][0]["reconcile_required"]
    assert snapshot()["generation"] > value["generation"]


def test_coordination_handoff_requires_owner_and_executable_next_action(team, monkeypatch):
    monkeypatch.setattr(h, "own_terminal", lambda *_: None)
    h.send("demo", "boss", "overlap", "owner overlap")
    with pytest.raises(typer.BadParameter, match="owner and exact next action"):
        inbox.disposition("demo", "overlap", "owner_handoff", "asked the worker",
                          ["message receipt"], "boss", None, None)


def test_blocked_anchor_never_attempts_pane_prompt_and_preserves_event(team, monkeypatch):
    data = h.load("demo")
    del data["members"]["boss"]
    data.update(anchor_terminal="native-supervisor", anchor_thread=THREAD)
    inbox.path_for(THREAD).unlink()
    with h.locked("demo"):
        h.save("demo", data)
    monkeypatch.setattr(h.tui, "panes", lambda *_: [{"terminal_id": "native-supervisor",
        "pane_id": "w1:p1", "agent": "codex", "agent_status": "blocked"}])
    command = Mock()
    monkeypatch.setattr(h.tui, "command", command)
    h.send("demo", "supervisor", "blocked-prompt", "Need handover")
    assert snapshot()["events"][0]["delivery"]["state"] == "pending"
    assert h.load("demo")["messages"]["blocked-prompt"]["state"] == "queued"
    command.assert_not_called()


def test_hook_applies_bound_role_objective_and_excludes_native_subagents(team, tmp_path):
    import io

    from agentroute.audit import AuditStore
    from agentroute.hook import codex_user_prompt_submit
    config = default_config()
    config.routing.switching.downgrade_confidence = 0
    store = AuditStore(tmp_path / "audit.jsonl")
    payload = {"session_id": THREAD, "turn_id": "short", "model": "gpt-5.6-terra",
               "prompt": "list files"}
    out = io.StringIO()
    assert codex_user_prompt_submit(io.StringIO(json.dumps(payload)), out,
                                    config=config, store=store) == 0
    rows = list(store.rows_since())
    assert "SUPERVISOR_CAPABILITY_FLOOR" in rows[-1]["reason_codes"]
    payload.update(agent_id="bounded-child", turn_id="child")
    assert codex_user_prompt_submit(io.StringIO(json.dumps(payload)), io.StringIO(),
                                    config=config, store=store) == 0
    assert "SUPERVISOR_CAPABILITY_FLOOR" not in list(store.rows_since())[-1]["reason_codes"]


def test_monitor_health_supersession_and_cooldown_keep_latest_actionable(team, monkeypatch):
    from agentroute import herdr_lifecycle as lifecycle
    data = h.load("demo")
    data["members"]["supervisor"] = data["members"].pop("boss")
    inbox.path_for(THREAD).unlink()
    with h.locked("demo"):
        h.save("demo", data)
    monkeypatch.setattr(h, "own_terminal", lambda *_: None)
    alerts = ["worker: blocked"]
    monkeypatch.setattr(lifecycle, "poll", lambda *_: {
        "team": "demo", "checked_at": 1, "alerts": list(alerts)})
    lifecycle.monitor("demo", False, True, 900, 300, 30)
    first = snapshot()["events"][0]["id"]
    alerts.append("worker: checkpoint overdue")
    lifecycle.monitor("demo", False, True, 900, 300, 30)
    value = snapshot()
    assert len(value["events"]) == 1 and value["events"][0]["id"] != first
    old = h.load("demo")["messages"][first]["coordination_event"]
    assert old["disposition"]["action"] == "superseded"
    assert old["disposition"]["evidence"]
    latest = value["events"][0]["id"]
    alerts.clear()
    lifecycle.monitor("demo", False, True, 900, 300, 30)
    assert not snapshot()["events"]
    assert h.load("demo")["messages"][latest]["coordination_event"]["disposition"][
        "action"] == "resolved"
    alerts.append("worker: blocked")
    lifecycle.monitor("demo", False, True, 900, 300, 30)
    assert snapshot()["events"][0]["id"] != first


def test_projection_rejects_symlink_and_repairs_directory_mode(team):
    path = inbox.path_for(THREAD)
    path.parent.chmod(0o755)
    with h.locked("demo") as data:
        h.save("demo", data)
    assert path.parent.stat().st_mode & 0o777 == 0o700
    path.unlink()
    path.symlink_to(path.parent / "elsewhere.json")
    with h.locked("demo") as data, pytest.raises(ValueError, match="symlinks"):
        h.save("demo", data)


def test_archived_children_do_not_overflow_and_wait_history_cannot_hide_actionable(team):
    data = h.load("demo")
    data["members"].pop("worker")
    for n in range(140):
        data["members"][f"old-{n}"] = {"spec": {"role": "worker"},
                                       "cleanup": {"state": "archived"}}
        key = f"wait-{n}"
        message = {"member": "boss", "from": "worker", "text": "Waiting", "state": "queued"}
        entry = inbox.event(key, message, "demo")
        entry.update(state="dispositioned", actionable=False, wait={"owner": "user",
            "delivery_state": "delivered", "question_ref": key, "resume_condition": "User answers"})
        message["coordination_event"] = entry
        data["messages"][key] = message
    message = {"member": "boss", "from": "worker", "text": "New overlap", "state": "queued"}
    message["coordination_event"] = inbox.event("new-overlap", message, "demo")
    data["messages"]["new-overlap"] = message
    with h.locked("demo"):
        h.save("demo", data)
    value = snapshot()
    assert value["live_children"] == []
    assert value["overflow"]["children"] == 0
    assert value["events"][0]["id"] == "new-overlap"
    assert value["overflow"]["events"] == 0
    assert value["overflow"]["waiting_events"] > 0
    data["members"]["current"] = {"spec": {"role": "worker"}, "launch": "started"}
    with h.locked("demo"):
        h.save("demo", data)
    assert [child["id"] for child in snapshot()["live_children"]] == ["current"]


def test_enrollment_is_explicit_bound_and_role_routing_does_not_depend_on_it(team, monkeypatch):
    with h.locked("demo") as data:
        data.pop("inbox_consumer")
        inbox.path_for(THREAD).unlink()
        h.save("demo", data)
    monkeypatch.setattr(h, "own_terminal", Mock())
    h.send("demo", "boss", "before-enable", "actionable report")
    assert not inbox.path_for(THREAD).exists()
    assert inbox.routing_context(THREAD)["role"] == "supervisor"
    assert h.load("demo")["messages"]["before-enable"]["delivery_hold"] == (
        "native_inbox_enrollment_required")
    with pytest.raises(typer.BadParameter, match="Version 1"):
        inbox.inbox_enable("demo", 2, ["native-receipt"])
    inbox.inbox_enable("demo", 1, ["native-build-sha+canary-receipt"])
    assert snapshot()["events"][0]["id"] == "before-enable"
    generation = snapshot()["generation"]
    inbox.inbox_enable("demo", 1, ["native-build-sha+canary-receipt"])
    assert snapshot()["generation"] == generation
    with pytest.raises(typer.BadParameter, match="different binding/evidence"):
        inbox.inbox_enable("demo", 1, ["different-native"])
    h.own_terminal.assert_not_called()


def test_execution_ready_preparation_and_assigned_coordination_feed_stop_guard(team, monkeypatch):
    items = {"parent": {"id": "parent", "next_step": {"state": "active"}},
             "prep": {"id": "prep", "next_action": "Prepare authorized source"},
             "overlap": {"id": "overlap", "next_step": {"state": "coordination_required"}}}
    ready = ["prep"]

    def packet_command(data, action, *args):
        if action == "next":
            return json.dumps([{"packet": "unrelated", "id": "ignored"}] + [
                {"packet": "packet", "id": key} for key in ready])
        key = args[-1]
        return json.dumps({"protocol_version": 1, "namespace": "markster", "packet": "packet",
            "item": key, "revision": 1, "actionable": key != "parent",
            "claimable": key == "prep", "coordination_required": key == "overlap",
            "assignment": {"session": "existing-worker"} if key == "overlap" else None,
            "next_step": {"id": "prepare", "action": "Resolve exact file owner", "state":
                "coordination_required" if key == "overlap" else "ready"},
            "outcome_wait": {"state": "awaiting_user", "question_ref": "delivered-question"}})

    monkeypatch.setattr(h, "packet_command", packet_command)
    with h.locked("demo") as data:
        inbox.sync_packet_steps(data, items, 1)
        h.save("demo", data)
    value = snapshot()
    assert len(value["events"]) == 2
    assert all(entry["kind"] == "packet_step" for entry in value["events"])
    assert {child["id"] for child in value["live_children"] if child["state"] == "queued"} == {
        "packet:packet/prep", "packet:packet/overlap"}
    messages = h.load("demo")["messages"]
    assert any(message["execution"]["outcome_wait"]["question_ref"] == "delivered-question"
               for message in messages.values())
    # Actual readiness/ownership change supersedes the event; a recorded disposition alone
    # cannot erase the queued executable step guard.
    ready.clear()
    items["overlap"]["next_step"]["state"] = "active"
    with h.locked("demo") as data:
        inbox.sync_packet_steps(data, items, 2)
        h.save("demo", data)
    assert not snapshot()["events"]
    assert not any(child["state"] == "queued" for child in snapshot()["live_children"])


def test_ledger_directory_fsync_precedes_native_publish(team, monkeypatch):
    order = []
    original = inbox.atomic_write
    monkeypatch.setattr(h.os, "fsync", lambda descriptor: order.append("fsync"))
    monkeypatch.setattr(inbox, "publish_roles", lambda _: order.append("roles"))
    monkeypatch.setattr(inbox, "publish", lambda _: order.append("publish"))
    with h.locked("demo") as data:
        h.save("demo", data)
    assert order == ["fsync", "fsync", "roles", "publish"]
    monkeypatch.setattr(inbox, "atomic_write", original)


@pytest.mark.parametrize("field", ["evidence", "question_ref", "resume_condition"])
def test_whitespace_wait_receipts_are_rejected(team, monkeypatch, field):
    monkeypatch.setattr(h, "own_terminal", lambda *_: None)
    h.send("demo", "boss", "wait", "approval")
    args = {"team": "demo", "event_id": "wait", "action": "awaiting_user",
            "reason": "Need approval",
            "evidence": ["plan"], "member": "boss", "question_ref": "question",
            "resume_condition": "Explicit approval"}
    args[field] = [" "] if field == "evidence" else " "
    with pytest.raises(typer.BadParameter):
        inbox.disposition(**args)
    with pytest.raises(typer.BadParameter):
        inbox.inbox_resume("demo", "wait", [" "], "boss")
