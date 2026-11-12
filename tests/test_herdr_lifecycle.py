import json
from unittest.mock import Mock

import pytest
import typer

from agentroute import herdr_lifecycle as lifecycle
from agentroute import herdr_team as h


@pytest.fixture
def team(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "state_dir", lambda: tmp_path)
    data = {"id": "demo", "namespace": "markster", "packet": "packet", "item": "other",
            "core_session": "ar123", "tui_session": "agentroute", "messages": {},
            "members": {"worker": {"attempt": "one", "launch": "started", "agent": "codex",
                "thread": "11111111-1111-1111-1111-111111111111",
                "packet_binding": {"item": "owned", "session": "owner", "brief": "fresh"},
                "spec": {"role": "worker", "cwd": "/tmp", "task": "task", "parent": None}}}}
    h.path_for("demo").parent.mkdir(parents=True)
    h.save("demo", data)
    return data


def test_resume_keeps_binding_and_new_terminal_item_label(team, monkeypatch):
    monkeypatch.setattr(h, "own_terminal", lambda *_: {"state": "finished"})
    calls = []
    monkeypatch.setattr(h, "core", lambda _, *args: calls.append(args) or {"address": "t9"})
    monkeypatch.setattr(h, "view", lambda *_: None)
    h.resume("demo", "worker")
    assert h.load("demo")["members"]["worker"]["packet_binding"] == {
        "item": "owned", "session": "owner", "brief": "fresh"}
    assert "item=owned" in calls[0]
    assert calls[0][-2:] == ("resume", team["members"]["worker"]["thread"])


def setup_poll(monkeypatch, item, terminal):
    monkeypatch.setattr(h, "packet_command", lambda _, action, *args: json.dumps(
        [] if action == "next" else {"items": [item]}))
    monkeypatch.setattr(h, "own_terminal", lambda *_: terminal)
    monkeypatch.setattr(h, "core", lambda *_: {"text": terminal.get("screen", "")})


def test_output_changes_and_checkpoint_age_are_independent(team, monkeypatch):
    item = {"id": "owned", "status": "active", "checkpoint": {"at": "1970-01-01T00:00:01Z"}}
    terminal = {"address": "t1", "state": "live", "status": {"state": "working"}, "screen": "a"}
    setup_poll(monkeypatch, item, terminal)
    assert lifecycle.poll("demo", 60, 30, now=2)["alerts"] == []
    assert "worker: observed activity overdue" in lifecycle.poll("demo", 60, 30, now=63)["alerts"]
    terminal["screen"] = "b"
    alerts = lifecycle.poll("demo", 60, 30, now=64)["alerts"]
    assert "worker: observed activity overdue" not in alerts
    assert "worker: canonical checkpoint overdue" in alerts


def test_idle_is_not_completion_and_missing_terminal_alerts(team, monkeypatch):
    setup_poll(monkeypatch, {"id": "owned", "status": "active"},
               {"address": "t1", "state": "live", "status": {"state": "done"}})
    lifecycle.poll("demo", 60, 30, now=1)
    assert "worker: waiting for follow-up; packet is not complete" in lifecycle.poll(
        "demo", 60, 30, now=32)["alerts"]
    monkeypatch.setattr(h, "own_terminal", lambda *_: None)
    assert "worker: terminal missing/exited; reconcile before resume" in lifecycle.poll(
        "demo", 60, 30, now=33)["alerts"]


def test_ack_requires_recipient_thread_and_is_idempotent(team, monkeypatch):
    data = h.load("demo")
    data["messages"]["m1"] = {"member": "worker", "state": "submitted", "created_at": 1}
    h.save("demo", data)
    with pytest.raises(typer.BadParameter):
        lifecycle.ack("demo", "m1")
    monkeypatch.setenv("AGENTROUTE_HERDR_TEAM", "demo")
    monkeypatch.setenv("AGENTROUTE_HERDR_MEMBER", "worker")
    monkeypatch.setenv("CODEX_THREAD_ID", team["members"]["worker"]["thread"])
    lifecycle.ack("demo", "m1")
    original = h.load("demo")["messages"]["m1"]["acknowledgment"]
    lifecycle.ack("demo", "m1")
    assert h.load("demo")["messages"]["m1"]["acknowledgment"] == original


def test_unacked_messages_alert_but_legacy_and_health_do_not_loop(team, monkeypatch):
    data = h.load("demo")
    data["messages"] = {
        "new": {"member": "worker", "state": "submitted", "created_at": 1, "submitted_at": 1},
        "old": {"member": "worker", "state": "submitted"},
        "health-test": {"member": "supervisor", "state": "submitted", "created_at": 1}}
    h.save("demo", data)
    setup_poll(monkeypatch, {"id": "owned"},
               {"address": "t1", "state": "live", "status": {"state": "working"}})
    alerts = lifecycle.poll("demo", 60, 30, now=32)["alerts"]
    assert alerts == ["message new to worker: receipt unacknowledged"]


def complete_item():
    return {"id": "owned", "status": "done", "revision": 4, "assignment": None,
            "criteria": [{"evidence": ["receipt"]}], "operations": {},
            "gates": {k: {"status": "passed"} for k in [
                "implementation", "checks", "merge", "deployment", "acceptance",
                "handoff", "cleanup"]}}


@pytest.mark.parametrize("defect", ["active", "assigned", "gate", "operation", "criterion"])
def test_cleanup_refuses_incomplete_evidence(team, monkeypatch, defect):
    item = complete_item()
    if defect == "active":
        item["status"] = "active"
    elif defect == "assigned":
        item["assignment"] = {"session": "owner"}
    elif defect == "gate":
        item["gates"]["acceptance"] = None
    elif defect == "operation":
        item["operations"] = {"op": {"state": "unknown"}}
    else:
        item["criteria"][0]["evidence"] = []
    setup_poll(monkeypatch, item, {"state": "finished", "address": "t1"})
    with pytest.raises(typer.BadParameter):
        lifecycle.cleanup("demo", "worker", True)


def test_cleanup_never_kills_live_worker_and_retains_thread(team, monkeypatch):
    terminal = {"state": "live", "address": "t1", "status": {"state": "idle"}}
    setup_poll(monkeypatch, complete_item(), terminal)
    core = Mock()
    monkeypatch.setattr(h, "core", core)
    with pytest.raises(typer.BadParameter, match="exit"):
        lifecycle.cleanup("demo", "worker", True)
    terminal["state"] = "finished"
    monkeypatch.setattr(h.tui, "panes", lambda _: [])
    lifecycle.cleanup("demo", "worker", True)
    current = h.load("demo")["members"]["worker"]
    assert current["cleanup"]["state"] == "archived"
    assert current["thread"] == team["members"]["worker"]["thread"]
    assert current["packet_binding"] == team["members"]["worker"]["packet_binding"]
    core.assert_not_called()


def test_cleanup_refuses_repurposed_pane(team, monkeypatch):
    data = h.load("demo")
    data["members"]["worker"]["view"] = {"terminal_id": "view", "pid": 99}
    h.save("demo", data)
    setup_poll(monkeypatch, complete_item(), {"state": "finished", "address": "t1"})
    monkeypatch.setattr(h.tui, "panes", lambda _: [{"terminal_id": "view", "pane_id": "w1:p1"}])
    command = Mock(return_value={"process_info": {"foreground_processes": [{"pid": 88}]}})
    monkeypatch.setattr(h.tui, "command", command)
    with pytest.raises(typer.BadParameter, match="ownership changed"):
        lifecycle.cleanup("demo", "worker", True)
    assert command.call_count == 1
    assert h.load("demo")["members"]["worker"]["cleanup"]["state"] == "prepared"


def test_monitor_alert_is_deduplicated_and_does_not_send_without_notify(team, monkeypatch):
    monkeypatch.setattr(lifecycle, "poll", lambda *_: {
        "alerts": ["worker: blocked"], "team": "demo"})
    send = Mock()
    monkeypatch.setattr(h, "send", send)
    lifecycle.monitor("demo", False, False, 900, 300, 30)
    send.assert_not_called()
    lifecycle.monitor("demo", False, True, 900, 300, 30)
    lifecycle.monitor("demo", False, True, 900, 300, 30)
    assert send.call_count == 1


def test_unknown_notification_is_not_replayed(team, monkeypatch):
    monkeypatch.setattr(lifecycle, "poll", lambda *_: {
        "alerts": ["worker: blocked"], "team": "demo"})

    def unknown_send(team, recipient, key, text, **kwargs):
        data = h.load(team)
        data["messages"][key] = {"state": "unknown", "member": recipient}
        h.save(team, data)
        raise RuntimeError("timeout")

    send = Mock(side_effect=unknown_send)
    monkeypatch.setattr(h, "send", send)
    with pytest.raises(RuntimeError):
        lifecycle.monitor("demo", False, True, 900, 300, 30)
    lifecycle.monitor("demo", False, True, 900, 300, 30)
    assert send.call_count == 1


def test_cleanup_closes_only_owned_view_and_preserves_receipts(team, monkeypatch):
    data = h.load("demo")
    data["members"]["worker"]["view"] = {"terminal_id": "view", "pid": 99}
    data["messages"]["receipt"] = {"state": "submitted"}
    h.save("demo", data)
    setup_poll(monkeypatch, complete_item(), {"state": "finished", "address": "t1"})
    monkeypatch.setattr(h.tui, "panes", lambda _: [{"terminal_id": "view", "pane_id": "w1:p1"}])
    command = Mock(return_value={"process_info": {"foreground_processes": [{"pid": 99}]}})
    monkeypatch.setattr(h.tui, "command", command)
    lifecycle.cleanup("demo", "worker", False)
    assert not command.called
    lifecycle.cleanup("demo", "worker", True)
    command.assert_any_call("agentroute", "pane", "close", "w1:p1")
    assert "receipt" in h.load("demo")["messages"]



def test_recipient_ack_reconciles_unknown_transport(team, monkeypatch):
    data = h.load("demo")
    data["messages"]["m1"] = {"member": "worker", "state": "unknown", "created_at": 1}
    h.save("demo", data)
    monkeypatch.setenv("AGENTROUTE_HERDR_TEAM", "demo")
    monkeypatch.setenv("AGENTROUTE_HERDR_MEMBER", "worker")
    monkeypatch.setenv("CODEX_THREAD_ID", team["members"]["worker"]["thread"])
    lifecycle.ack("demo", "m1")
    message = h.load("demo")["messages"]["m1"]
    assert message["state"] == "submitted"
    assert message["delivery_reconciliation"]


def test_supervisor_report_never_reaches_prompt_refusal(team, monkeypatch):
    data = h.load("demo")
    data["anchor_terminal"] = "supervisor-terminal"
    h.save("demo", data)
    monkeypatch.setattr(h.tui, "panes", lambda _: [{
        "terminal_id": "supervisor-terminal", "pane_id": "w1:p1", "agent": "codex"}])
    monkeypatch.setattr(h.tui, "command", Mock(side_effect=RuntimeError(json.dumps({
        "error": {"code": "agent_blocked", "message": "requires interactive input"}}))))
    h.send("demo", "supervisor", "blocked-report", "Report")
    message = h.load("demo")["messages"]["blocked-report"]
    assert message["state"] == "queued"
    h.tui.command.assert_not_called()
    assert message["delivery_hold"] == "native_inbox_enrollment_required"


def test_blocked_supervisor_holds_notifications_but_inspection_continues(team, monkeypatch):
    data = h.load("demo")
    data["anchor_terminal"] = "supervisor-terminal"
    data["messages"]["legacy-unknown"] = {"member": "supervisor", "state": "unknown"}
    h.save("demo", data)
    monkeypatch.setattr(h.tui, "panes", lambda _: [{
        "terminal_id": "supervisor-terminal", "agent_status": "blocked"}])
    setup_poll(monkeypatch, {"id": "owned"},
               {"address": "t1", "state": "live", "status": {"state": "working"}})
    result = lifecycle.poll("demo", 900, 300)
    assert "message legacy-unknown: delivery unknown; inspect, do not resend" in result["alerts"]
    assert h.load("demo")["monitor"]["supervisor_state"] == "blocked"
    send = Mock()
    monkeypatch.setattr(h, "send", send)
    lifecycle.monitor("demo", False, True, 900, 300, 30)
    send.assert_called_once()
    assert send.call_args.kwargs["notify"] is False
    assert h.load("demo")["members"]["worker"]["health"]["checked_at"]


def released_step_item():
    return {"id": "owned", "status": "blocked", "revision": 182, "assignment": None,
            "next_step": {"id": "source", "state": "done", "kind": "work"},
            "last_release": {"session": "owner", "at": "2026-11-11T00:00:00Z"},
            "outcome_wait": {"state": "waiting_external", "reason": "Later recovery approval"},
            "operations": {}, "criteria": [{"id": "full", "evidence": []}], "gates": {}}


def test_step_retirement_dry_run_and_partial_archive_preserve_full_hold(team, monkeypatch):
    item = released_step_item()
    before = json.loads(json.dumps(item))
    setup_poll(monkeypatch, item, {"state": "finished", "address": "t1"})
    monkeypatch.setattr(h.tui, "panes", lambda _: [])
    command = Mock()
    monkeypatch.setattr(h.tui, "command", command)
    lifecycle.retire_step("demo", "worker", "source", 182, ["merged-source-receipt"], False)
    assert "cleanup" not in h.load("demo")["members"]["worker"]
    lifecycle.retire_step("demo", "worker", "source", 182, ["merged-source-receipt"], True)
    saved = h.load("demo")["members"]["worker"]
    assert saved["thread"] == team["members"]["worker"]["thread"]
    assert saved["packet_binding"] == team["members"]["worker"]["packet_binding"]
    assert saved["cleanup"]["kind"] == "step_handoff"
    assert saved["cleanup"]["outcome_complete"] is False
    assert saved["cleanup"]["step"] == item["next_step"]
    assert saved["cleanup"]["last_release"] == item["last_release"]
    assert saved["cleanup"]["outcome_wait"] == item["outcome_wait"]
    assert saved["cleanup"]["state"] == "archived" and item == before
    command.assert_not_called()
    lifecycle.retire_step("demo", "worker", "source", 182, ["same receipt"], True)
    with pytest.raises(typer.BadParameter, match="identity changed"):
        lifecycle.retire_step("demo", "worker", "other-step", 182, ["receipt"], True)
    with pytest.raises(typer.BadParameter, match="identity changed"):
        lifecycle.retire_step("demo", "worker", "source", 183, ["receipt"], True)


@pytest.mark.parametrize("defect", ["active", "assigned", "empty_assignment", "owner", "step",
                                    "revision", "pending",
                                    "live", "missing", "full-cleanup", "attempt"])
def test_step_retirement_rejects_unreleased_or_mismatched_state(team, monkeypatch, defect):
    item = released_step_item()
    terminal = {"state": "finished", "address": "t1"}
    if defect == "active":
        item["status"] = "active"
    if defect == "assigned":
        item["assignment"] = {"session": "reclaimed-owner"}
    if defect == "empty_assignment":
        item["assignment"] = {}
    if defect == "owner":
        item["last_release"]["session"] = "different-owner"
    if defect == "step":
        item["next_step"]["state"] = "active"
    if defect == "revision":
        item["revision"] = 183
    if defect == "pending":
        item["operations"] = {"held": {"state": "unknown"}}
    if defect == "live":
        terminal["state"] = "live"
    if defect == "missing":
        terminal = None
    setup_poll(monkeypatch, item, terminal)
    monkeypatch.setattr(h.tui, "panes", lambda _: [])
    command = Mock()
    monkeypatch.setattr(h.tui, "command", command)
    if defect == "full-cleanup":
        with h.locked("demo") as data:
            data["members"]["worker"]["cleanup"] = {"kind": "outcome", "state": "archived"}
            h.save("demo", data)
    if defect == "attempt":
        lifecycle.retire_step("demo", "worker", "source", 182, ["receipt"], True)
        with h.locked("demo") as data:
            data["members"]["worker"]["attempt"] = "new-attempt"
            h.save("demo", data)
    with pytest.raises(typer.BadParameter):
        lifecycle.retire_step("demo", "worker", "source", 182, ["receipt"], True)
    command.assert_not_called()


def test_step_retirement_unknown_close_requires_inspected_exact_absence(team, monkeypatch):
    with h.locked("demo") as data:
        data["members"]["worker"]["view"] = {"terminal_id": "view", "pane_id": "w1:p1", "pid": 99}
        h.save("demo", data)
    setup_poll(monkeypatch, released_step_item(), {"state": "finished", "address": "t1"})
    panes = [{"terminal_id": "view", "pane_id": "w1:p1"}]
    monkeypatch.setattr(h.tui, "panes", lambda _: list(panes))
    command = Mock(side_effect=[{"process_info": {"foreground_processes": [{"pid": 99}]}},
                                {"process_info": {"foreground_processes": [{"pid": 99}]}},
                                RuntimeError("close response lost")])
    monkeypatch.setattr(h.tui, "command", command)
    with pytest.raises(RuntimeError, match="lost"):
        lifecycle.retire_step("demo", "worker", "source", 182, ["source receipt"], True)
    original = h.load("demo")["members"]["worker"]["cleanup"]
    assert original["state"] == "unknown"
    fingerprint = original["intent_fingerprint"]
    command.reset_mock()
    with pytest.raises(typer.BadParameter, match="unresolved"):
        lifecycle.retire_step("demo", "worker", "source", 182, ["receipt"], True)
    with pytest.raises(typer.BadParameter, match="still present"):
        lifecycle.retire_step("demo", "worker", "source", 182, ["inspection"],
                              True, True, fingerprint)
    panes[:] = [{"terminal_id": "replacement", "pane_id": "w1:p1"}]
    with pytest.raises(typer.BadParameter, match="reused"):
        lifecycle.retire_step("demo", "worker", "source", 182, ["inspection"],
                              True, True, fingerprint)
    panes.clear()
    with pytest.raises(typer.BadParameter, match="fingerprint"):
        lifecycle.retire_step("demo", "worker", "source", 182, ["inspection"], True, True, "wrong")
    lifecycle.retire_step("demo", "worker", "source", 182, ["verified absent"],
                          True, True, fingerprint)
    saved = h.load("demo")["members"]["worker"]["cleanup"]
    assert saved["state"] == "archived" and saved["outcome_complete"] is False
    assert saved["identity"] == original["identity"] and saved["step"] == original["step"]
    assert saved["reconciliation"]["evidence"] == ["verified absent"]
    command.assert_not_called()


@pytest.mark.parametrize("close_verified", [True, False])
def test_step_retirement_closes_only_owned_viewer_and_requires_absence(team, monkeypatch,
                                                                      close_verified):
    with h.locked("demo") as data:
        data["members"]["worker"]["view"] = {"terminal_id": "view", "pane_id": "w1:p1", "pid": 99}
        h.save("demo", data)
    setup_poll(monkeypatch, released_step_item(), {"state": "finished", "address": "t1"})
    panes = [{"terminal_id": "view", "pane_id": "w1:p1"}]
    monkeypatch.setattr(h.tui, "panes", lambda _: list(panes))
    calls = []

    def command(session, *args):
        calls.append(args)
        if args[1] == "process-info":
            return {"process_info": {"foreground_processes": [{"pid": 99}]}}
        assert args == ("pane", "close", "w1:p1")
        assert h.load("demo")["members"]["worker"]["cleanup"]["state"] == "prepared"
        if close_verified:
            panes.clear()
        return {}

    monkeypatch.setattr(h.tui, "command", command)
    if close_verified:
        lifecycle.retire_step("demo", "worker", "source", 182, ["handoff receipt"], True)
    else:
        with pytest.raises(RuntimeError, match="not independently verified"):
            lifecycle.retire_step("demo", "worker", "source", 182, ["handoff receipt"], True)
    saved = h.load("demo")["members"]["worker"]["cleanup"]
    assert saved["state"] == ("archived" if close_verified else "unknown")
    assert calls == [("pane", "process-info", "--pane", "w1:p1"),
                     ("pane", "process-info", "--pane", "w1:p1"), ("pane", "close", "w1:p1")]


def test_step_retirement_refuses_replaced_viewer_before_intent(team, monkeypatch):
    with h.locked("demo") as data:
        data["members"]["worker"]["view"] = {"terminal_id": "view", "pane_id": "w1:p1", "pid": 99}
        h.save("demo", data)
    setup_poll(monkeypatch, released_step_item(), {"state": "finished", "address": "t1"})
    monkeypatch.setattr(h.tui, "panes", lambda _: [{"terminal_id": "view", "pane_id": "w1:p1"}])
    command = Mock(return_value={"process_info": {"foreground_processes": [{"pid": 88}]}})
    monkeypatch.setattr(h.tui, "command", command)
    with pytest.raises(typer.BadParameter, match="ownership changed"):
        lifecycle.retire_step("demo", "worker", "source", 182, ["receipt"], True)
    assert "cleanup" not in h.load("demo")["members"]["worker"]
    assert command.call_count == 1


@pytest.mark.parametrize("defect", [None, "active", "active-absent", "missing-active",
                                    "shell-pid", "group",
                                    "process", "name", "multiple", "changed-before-close"])
def test_step_retirement_explicit_inactive_idle_shell_guard(team, monkeypatch, defect):
    with h.locked("demo") as data:
        view = {"terminal_id": "view", "pane_id": "w1:p1", "pid": 99, "active": False}
        if defect in {"active", "active-absent"}:
            view["active"] = True
        if defect == "missing-active":
            view.pop("active")
        data["members"]["worker"]["view"] = view
        h.save("demo", data)
    setup_poll(monkeypatch, released_step_item(), {"state": "finished", "address": "t1"})
    panes = [] if defect == "active-absent" else [{"terminal_id": "view", "pane_id": "w1:p1"}]
    monkeypatch.setattr(h.tui, "panes", lambda _: list(panes))
    inspected = {"pane_id": "w1:p1", "shell_pid": 77, "foreground_process_group_id": 77,
                 "foreground_processes": [{"pid": 77, "name": "zsh"}]}
    if defect == "shell-pid":
        inspected["shell_pid"] = 78
    if defect == "group":
        inspected["foreground_process_group_id"] = 78
    if defect == "process":
        inspected["foreground_processes"][0]["pid"] = 78
    if defect == "name":
        inspected["foreground_processes"][0]["name"] = "python"
    if defect == "multiple":
        inspected["foreground_processes"].append({"pid": 78, "name": "zsh"})
    calls = []

    def command(session, *args):
        calls.append(args)
        if args[1] == "process-info":
            if defect == "changed-before-close" and len(calls) == 2:
                return {"process_info": {**inspected, "shell_pid": 78}}
            return {"process_info": inspected}
        assert args == ("pane", "close", "w1:p1")
        panes.clear()
        return {}

    monkeypatch.setattr(h.tui, "command", command)
    if defect:
        with pytest.raises(typer.BadParameter, match="idle-shell"):
            lifecycle.retire_step("demo", "worker", "source", 182, ["inspected original shell"],
                                  True, idle_shell_pid=77)
        assert not any(args[1] == "close" for args in calls)
        assert "cleanup" not in h.load("demo")["members"]["worker"]
    else:
        with pytest.raises(typer.BadParameter, match="ownership changed"):
            lifecycle.retire_step("demo", "worker", "source", 182, ["receipt"], True)
        lifecycle.retire_step("demo", "worker", "source", 182, ["inspected original shell"],
                              True, idle_shell_pid=77)
        saved = h.load("demo")["members"]["worker"]["cleanup"]
        assert saved["state"] == "archived" and saved["outcome_complete"] is False
        assert saved["identity"]["ownership"] == {"mode": "idle_shell", "idle_shell_pid": 77}
        with pytest.raises(typer.BadParameter, match="identity changed"):
            lifecycle.retire_step("demo", "worker", "source", 182, ["receipt"],
                                  True, idle_shell_pid=78)


def test_monitor_reconciles_admitted_delivery_receipt(team, monkeypatch, tmp_path):
    from agentroute import supervisor_inbox as inbox

    monkeypatch.setattr(inbox, "codex_home", lambda: tmp_path / "codex")
    data = h.load("demo")
    supervisor = "22222222-2222-4222-8222-222222222222"
    data["members"]["boss"] = {"thread": supervisor, "attempt": "boss",
        "spec": {"role": "supervisor"}}
    data["inbox_consumer"] = {"version": 1, "evidence": ["test"]}
    h.save("demo", data)
    h.send("demo", "boss", "report", "Worker completed the check")
    path = inbox.codex_home() / "supervisor-delivery-receipts" / supervisor / "batch.json"
    inbox.atomic_write(path, {"version": 1, "thread_id": supervisor, "turn_id": "turn",
                              "admission_token": "batch", "event_ids": ["report"]})
    path.parent.parent.chmod(0o700)
    setup_poll(monkeypatch, {"id": "owned"},
               {"address": "t1", "state": "live", "status": {"state": "working"}})
    lifecycle.poll("demo", 900, 300)
    entry = h.load("demo")["messages"]["report"]["coordination_event"]
    assert entry["state"] == "delivered" and entry["actionable"]
    assert entry["delivery"]["turn_id"] == "turn"
