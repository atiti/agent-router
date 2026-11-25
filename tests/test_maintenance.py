import json
import time
from datetime import datetime, timezone
from unittest.mock import Mock

from agentroute import maintenance as m
from agentroute import waits

THREAD = "01a20744-50ee-71a2-a6a3-3e26905fc41b"
KEY = ("private", "fixture", "handoff")


def item(now):
    return {
        "id": KEY[-1], "revision": 7, "status": "active",
        "next_step": {"state": "active"},
        "assignment": {"session": THREAD,
                       "at": datetime.fromtimestamp(now-4000, timezone.utc).isoformat()},
    }


def test_classification_distinguishes_user_hold_uncertain_effect_and_ready_owner():
    now = time.time()
    row = item(now)
    assert m.classify(row, now) == ("work_due", None)
    row["operations"] = {"deploy": {"state": "unknown"}}
    assert m.classify(row, now) == ("unresolved_operation", "verifier")
    row["next_step"] = {"state": "awaiting_user"}
    assert m.classify(row, now) == ("awaiting_user", None)
    row["next_step"], row["operations"], row["assignment"] = {"state": "active"}, {}, None
    assert m.classify(row, now) == ("no_assignment", None)


def test_plain_due_conversations_are_reported_without_steering(monkeypatch):
    row = item(time.time())
    monkeypatch.setattr(m, "packet_rows", lambda: iter([(KEY, {"items": [row]}, row)]))
    monkeypatch.setattr(m, "_rows", lambda *args: [{"id": THREAD}])
    monkeypatch.setattr(waits, "load", lambda: [])
    register = Mock()
    monkeypatch.setattr(waits, "register", register)
    assert m.reconcile_packets([], time.time())["counts"] == {"needs_idle_admission": 1}
    row["goal_review"] = {"advances_goal": False}
    assert m.reconcile_packets([], time.time())["counts"] == {"requires_replan": 1}
    register.assert_not_called()


def test_only_one_exact_live_enrolled_monitor_can_be_recovered(tmp_path, monkeypatch):
    a = {"id": "current", "namespace": "markster", "packet": "ivan",
         "inbox_consumer": {"version": 1}}
    b = {**a, "id": "old"}
    monkeypatch.setattr(m.herdr_team, "path_for", lambda team: tmp_path / team / "team.json")
    monkeypatch.setattr(m, "live_supervisors", lambda t: int(t["id"] == "current"))
    start = Mock()
    monkeypatch.setattr(m.herdr_lifecycle, "start_monitor", start)
    assert m.reconcile_monitors([a, b], False) == [{"team": "current", "state": "eligible"}]
    start.assert_not_called()
    assert m.reconcile_monitors([a, b], True) == [{"team": "current", "state": "start_requested"}]
    start.assert_called_once_with("current")
    monkeypatch.setattr(m, "live_supervisors", lambda _: 1)
    assert m.reconcile_monitors([a, b], True)[0]["state"] == "ambiguous"
    start.assert_called_once()


def test_periodic_tick_recovers_wait_reader_but_dry_run_does_not_write_status(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(m, "status_path", lambda: tmp_path / "status.json")
    monkeypatch.setattr(m, "packet_rows", lambda: iter(()))
    monkeypatch.setattr(m, "read_teams", lambda: [])
    monkeypatch.setattr(waits, "load", lambda: [{"state": "pending", "thread_id": THREAD}])
    monkeypatch.setattr(waits, "daemon_running", lambda: False)
    start = Mock(return_value=True)
    monkeypatch.setattr(waits, "ensure_daemon", start)
    m.run_once()
    start.assert_not_called()
    assert not m.status_path().exists()
    result = m.run_once(apply=True)
    assert result["waits"]["start_requested"]
    start.assert_called_once()
    assert json.loads(m.status_path().read_text())["waits"]["pending_or_fired"] == 1


def test_existing_delivered_decision_is_reported_instead_of_requested_again(monkeypatch):
    row = item(time.time())
    row["next_step"] = {"state": "awaiting_user"}
    monkeypatch.setattr(m, "packet_rows", lambda: iter([(KEY, {}, row)]))
    monkeypatch.setattr(m, "_rows", lambda *args: [])
    monkeypatch.setattr(waits, "load", lambda: [])
    monkeypatch.setattr(m.decisions, "recorded", lambda key: [
        {"id": "old", "slot": "step", "revision": 6, "turn_id": "old"},
        {"id": "receipt", "slot": "step", "revision": 7, "turn_id": "turn"},
    ])
    verify = Mock(return_value={"id": "receipt", "state": "delivered"})
    monkeypatch.setattr(m.decisions, "verify_receipt", verify)
    report = m.reconcile_packets([], time.time())
    assert report["counts"] == {"decision_delivered": 1}
    verify.assert_called_once_with(KEY, "receipt", None)
    row["outcome_wait"] = {"state": "awaiting_user", "owner": "Operator",
                           "question_ref": "distinct-question"}
    report = m.reconcile_packets([], time.time())
    assert report["counts"] == {"awaiting_user": 1}
    entry, = report["items"]
    assert entry["feedback"]["step"]["state"] == "decision_delivered"
    assert entry["feedback"]["outcome"]["state"] == "answer_needed"
    assert entry["owner"] == THREAD and entry["binding"] == "unverified"


def test_unassigned_wait_keeps_its_review_condition():
    row = {"status": "blocked", "next_step": {"state": "waiting_worker"}}
    assert m.classify(row, time.time()) == ("wait_without_review", None)
    row["next_step"] = {}
    assert m.classify(row, time.time()) == ("blocker_needs_owner", "reviewer")
