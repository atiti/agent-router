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
