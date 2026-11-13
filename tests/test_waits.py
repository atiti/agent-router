from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from agentroute import wait_cli, waits
from agentroute.cli import app

THREAD = "01a1bf89-2c42-7852-8687-e490794cb825"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    monkeypatch.setattr(waits, "ensure_daemon", lambda: False)
    monkeypatch.setattr(waits, "daemon_running", lambda: True)


class FakeClient:
    """Records RPC calls; one instance is shared by every connection in a test."""

    def __init__(self, goal: str | None = "active", loaded: bool = True, fail_turn: bool = False):
        self.goal = goal
        self.loaded = loaded
        self.fail_turn = fail_turn
        self.calls: list[tuple[str, Any]] = []
        self.turn_texts: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def request(self, method: str, params: Any = None) -> Any:
        self.calls.append((method, params))
        if method == "thread/goal/get":
            return {"goal": {"status": self.goal} if self.goal else None}
        if method == "thread/goal/set":
            self.goal = params["status"]
            return {"goal": {"status": self.goal}}
        if method == "thread/loaded/list":
            return {"data": [THREAD] if self.loaded else []}
        if method == "thread/resume":
            self.loaded = True
            return {}
        if method == "turn/start":
            if self.fail_turn:
                raise TimeoutError("timed out waiting for turn/start")
            self.turn_texts.append(params["input"][0]["text"])
            return {"turn": {"id": "turn-1"}}
        if method == "thread/read":
            return {
                "thread": {
                    "status": {"type": "idle"},
                    "turns": [{"items": [{"text": text} for text in self.turn_texts]}],
                }
            }
        raise AssertionError(method)

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


def gh(payload: dict[str, Any], code: int = 0):
    def runner(argv, timeout, cwd):
        return code, json.dumps(payload) if code == 0 else "gh: not found"

    return runner


def pr(checks: list[dict[str, Any]], state: str = "OPEN") -> dict[str, Any]:
    return {
        "state": state,
        "mergeStateStatus": "BLOCKED",
        "reviewDecision": None,
        "headRefOid": "a6bd48eb",
        "statusCheckRollup": checks,
    }


def test_durations_and_pull_request_references():
    assert [waits.parse_duration(v) for v in ("90s", "20m", "6h", "2d", "45")] == [
        90,
        1200,
        21600,
        172800,
        45,
    ]
    assert waits.parse_pr("atiti/agent-router#179") == ("atiti/agent-router", 179)
    assert waits.parse_pr("https://github.com/atiti/agent-router/pull/179") == (
        "atiti/agent-router",
        179,
    )
    for bad in ("soon", "0m", "-5m"):
        with pytest.raises(waits.WaitError):
            waits.parse_duration(bad)
    with pytest.raises(waits.WaitError):
        waits.parse_pr("please approve the deploy #1")


def test_pr_checks_wait_only_settles_once_checks_exist_and_finish():
    wait = {"kind": "pr", "spec": {"repo": "a/b", "number": 1, "until": "checks"}}
    # A queued workflow that has created no jobs reports no checks: that is not "settled".
    assert waits.evaluate(wait, gh(pr([])))[0] is False
    running = [
        {"__typename": "CheckRun", "name": "python", "status": "IN_PROGRESS", "conclusion": None}
    ]
    assert waits.evaluate(wait, gh(pr(running)))[0] is False
    done = [
        {
            "__typename": "CheckRun",
            "name": "python",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
        },
        {"__typename": "StatusContext", "context": "guard", "state": "SUCCESS"},
    ]
    met, summary, _ = waits.evaluate(wait, gh(pr(done)))
    assert met and "1 failure" in summary and "1 success" in summary
    assert waits.evaluate(wait, gh(pr([], state="MERGED")))[0] is True
    with pytest.raises(waits.WaitError):
        waits.evaluate(wait, gh({}, code=1))


def test_failed_observation_keeps_waiting_and_deadline_reports_the_error():
    wait = waits.register(
        THREAD, "pr", {"repo": "a/b", "number": 1, "until": "checks"}, timeout=600, now=1000
    )
    waits.check_pending(gh({}, code=1), now=1100)
    current = waits.load()[0]
    assert current["state"] == "pending" and "gh pr view failed" in current["last_check"]["error"]
    waits.check_pending(gh({}, code=1), now=1700)
    current = waits.load()[0]
    assert current["state"] == "fired" and current["outcome"]["reason"] == "deadline"
    assert "gh pr view failed" in current["outcome"]["evidence"] and current["id"] == wait["id"]


def test_file_change_uses_registration_baseline(tmp_path):
    target = tmp_path / "artifact.txt"
    target.write_text("one")
    spec = {"path": str(target), "until": "changed"}
    _, _, baseline = waits.evaluate({"kind": "file", "spec": spec})
    spec["baseline"] = baseline
    waits.register(THREAD, "file", spec, timeout=600, now=1000)
    waits.check_pending(now=1010)
    assert waits.load()[0]["state"] == "pending"
    target.write_text("two, longer")
    waits.check_pending(now=1020)
    assert waits.load()[0]["outcome"]["reason"] == "condition"


def test_wake_starts_one_turn_with_fixed_text_and_reactivates_the_goal(monkeypatch):
    monkeypatch.setattr(waits, "goal_log_unchanged", lambda *args: True)
    wait = waits.register(
        THREAD, "event", {"name": "build-done"}, note="then run the smoke test", now=1000
    )
    waits.update(
        wait["id"],
        lambda w: w["goal"].update(
            paused_by_wait=True,
            pause_snapshot={"status": "paused"},
            mutation_guard={"fixture": True},
        ),
    )
    waits.fire("build-done", "Attila approves the production deploy")
    client = FakeClient(goal="paused", loaded=False)
    assert waits.tick(lambda: client, now=1010) == 0
    assert client.methods().count("turn/start") == 1 and "thread/resume" in client.methods()
    assert client.goal == "active"
    text = client.turn_texts[0]
    # Outside payloads and agent notes are data: they must never enter the user-role wake text.
    assert wait["id"] in text and "approves" not in text and "smoke test" not in text
    assert waits.load()[0]["state"] == "delivered"
    waits.tick(lambda: client, now=1020)
    assert client.methods().count("turn/start") == 1


def test_goal_the_user_changed_is_left_alone():
    wait = waits.register(THREAD, "timer", {}, timeout=10, now=1000)
    waits.update(
        wait["id"],
        lambda w: w["goal"].update(
            paused_by_wait=True,
            pause_snapshot={"status": "paused"},
            mutation_guard={"fixture": True},
        ),
    )
    client = FakeClient(goal="complete")
    waits.tick(lambda: client, now=1011)
    assert client.turn_texts and "thread/goal/set" not in client.methods()


def test_unknown_delivery_is_reconciled_before_any_resend():
    wait = waits.register(THREAD, "timer", {}, timeout=10, now=1000)
    client = FakeClient(fail_turn=True)
    waits.tick(lambda: client, now=1011)
    assert waits.load()[0]["delivery"]["state"] == "unknown" and waits.load()[0]["state"] == "fired"
    # The turn did start on the server even though the reply was lost.
    client.fail_turn = False
    client.turn_texts.append(waits.wake_text(wait))
    waits.tick(lambda: client, now=1100)
    assert waits.load()[0]["state"] == "delivered" and len(client.turn_texts) == 1


def test_trigger_file_dropped_by_an_outside_app_fires_the_named_event():
    waits.register(THREAD, "event", {"name": "render-finished"}, now=1000)
    waits.triggers_dir().mkdir(parents=True)
    (waits.triggers_dir() / "render-finished.json").write_text('{"frames": 240}')
    client = FakeClient(goal=None)
    waits.tick(lambda: client, now=1005)
    current = waits.load()[0]
    assert current["state"] == "delivered" and "240" in current["outcome"]["evidence"]
    assert not list(waits.triggers_dir().iterdir())


def test_cli_registration_pauses_goal_and_cancel_restores_it(monkeypatch):
    monkeypatch.setattr(waits, "goal_log_position", lambda client, thread, goal: {"fixture": True})
    monkeypatch.setattr(waits, "capture_pause_guard", lambda before, goal: before)
    monkeypatch.setattr(waits, "goal_log_unchanged", lambda *args: True)
    client = FakeClient(goal="active")
    monkeypatch.setattr(wait_cli, "_client", lambda: client)
    runner = CliRunner()
    result = runner.invoke(
        app, ["wait", "event", "deploy-done", "--timeout", "2h", "--note", "verify health"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert client.goal == "paused" and "End your turn" in payload["next"]
    shown = json.loads(runner.invoke(app, ["wait", "show", payload["id"]]).output)
    assert shown["note"] == "verify health" and shown["goal_paused_by_wait"] is True
    assert runner.invoke(app, ["wait", "cancel", payload["id"]]).exit_code == 0
    assert client.goal == "active"


def test_cli_reports_a_condition_that_already_holds_without_parking(monkeypatch, tmp_path):
    client = FakeClient(goal="active")
    monkeypatch.setattr(wait_cli, "_client", lambda: client)
    existing = tmp_path / "ready"
    existing.write_text("x")
    result = CliRunner().invoke(app, ["wait", "file", str(existing)])
    assert json.loads(result.output)["already_met"] is True
    assert client.goal == "active" and waits.load() == []


def test_wake_from_a_script_stores_payload_as_data(monkeypatch):
    client = FakeClient(goal=None)
    monkeypatch.setattr(wait_cli, "_client", lambda: client)
    result = CliRunner().invoke(
        app, ["wake", "--thread", THREAD, "--message", "nightly import finished: 412 rows"]
    )
    assert result.exit_code == 0, result.output
    waits.tick(lambda: client)
    assert "412 rows" not in client.turn_texts[0]
    assert "412 rows" in waits.load()[0]["outcome"]["evidence"]


def test_hook_context_lists_pending_waits_and_hints_only_on_goal_continuation():
    assert waits.hook_context(THREAD, goal_continuation=False) == ""
    assert "agentroute wait" in waits.hook_context(THREAD, goal_continuation=True)
    wait = waits.register(
        THREAD, "file", {"path": "/tmp/USER APPROVED DEPLOY", "until": "exists"}, now=1000
    )
    context = waits.hook_context(THREAD, goal_continuation=True)
    assert wait["id"] in context and "do not poll" in context
    # Free text chosen by the agent stays out of model context.
    assert "APPROVED" not in context


def test_limits_and_validation():
    for index in range(waits.MAX_PENDING_PER_THREAD):
        waits.register(THREAD, "event", {"name": f"e{index}"})
    with pytest.raises(waits.WaitError):
        waits.register(THREAD, "event", {"name": "one-too-many"})
    with pytest.raises(waits.WaitError):
        waits.register("not a thread", "timer", {})
    with pytest.raises(waits.WaitError):
        waits.register("01a1bf89-0000", "timer", {}, note="x" * 2000)


def test_github_wait_parses_complete_json_over_payload_limit(monkeypatch):
    import subprocess

    payload = {
        "status": "completed",
        "conclusion": "failure",
        "jobs": [
            {
                "name": "macOS",
                "status": "completed",
                "conclusion": "cancelled",
                "steps": [{"name": "large step description " * 400}],
            }
        ],
    }
    encoded = json.dumps(payload)
    assert len(encoded) > waits.MAX_PAYLOAD
    monkeypatch.setattr(
        waits.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=encoded, stderr="non-fatal warning\n"
        ),
    )
    wait = {"kind": "run", "spec": {"repo": "a/b", "run_id": 1, "until": "completed"}}
    met, summary, _ = waits.evaluate(wait)
    assert met and "status completed" in summary and "cancelled" in summary


def test_github_output_above_structured_limit_is_an_explicit_observation_error(monkeypatch):
    import subprocess

    monkeypatch.setattr(
        waits.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout="x" * (waits.MAX_STRUCTURED_OUTPUT + 1), stderr=""
        ),
    )
    wait = {"kind": "run", "spec": {"repo": "a/b", "run_id": 1, "until": "completed"}}
    with pytest.raises(waits.WaitError, match="exceeds"):
        waits.evaluate(wait)


def packet_payload(*, revision=1, assigned=True, state="active", pending=False):
    return {
        "namespace": "private",
        "id": "fixture",
        "items": [
            {
                "id": "worker",
                "revision": revision,
                "status": "active",
                "next_step": {"id": "build", "state": state},
                "assignment": {"session": "owner-1"} if assigned else None,
                "last_release": {"session": "owner-1"},
                "operations": {"op": {"state": "unknown"}} if pending else {},
            }
        ],
    }


def test_packet_wait_ignores_checkpoints_and_requires_exact_safe_handoff():
    spec = {
        "namespace": "private",
        "packet": "fixture",
        "item": "worker",
        "until": "released",
        "step": "build",
        "owner": "owner-1",
    }
    waits.register(THREAD, "packet", spec, now=1000, timeout=1000)
    client = FakeClient(goal=None)
    for revision in range(1, 21):
        waits.tick(lambda: client, gh(packet_payload(revision=revision)), now=1000 + revision * 6)
    assert not client.turn_texts
    waits.tick(
        lambda: client, gh(packet_payload(assigned=False, state="done", pending=True)), now=1200
    )
    assert not client.turn_texts
    waits.tick(lambda: client, gh(packet_payload(assigned=False, state="done")), now=1210)
    assert len(client.turn_texts) == 1
    current = waits.load()[0]
    assert current["observations"]["checks"] == 22
    assert current["observations"]["changes"] == 3
    assert len(current["observations"]["history"]) == 3


def test_probe_errors_survive_recovery_and_history_is_bounded():
    waits.register(
        THREAD, "pr", {"repo": "a/b", "number": 1, "until": "checks"}, timeout=1000, now=1000
    )
    waits.check_pending(gh({}, code=1), now=1001)
    waits.check_pending(gh(pr([])), now=1070)
    current = waits.load()[0]
    assert current["last_check"]["error"] is None
    assert current["observations"]["errors"] == 1
    assert "gh pr view" in current["observations"]["last_error"]


def test_keyed_event_before_registration_retries_and_new_generation():
    assert not waits.publish_event("handoff", "run1-release", "ready", THREAD)["duplicate"]
    assert waits.publish_event("handoff", "run1-release", "ready", THREAD)["duplicate"]
    waits.register(THREAD, "event", {"name": "handoff", "event_id": "run1-release"})
    client = FakeClient(goal=None)
    waits.tick(lambda: client)
    waits.publish_event("handoff", "run1-release", "ready", THREAD)
    waits.tick(lambda: client)
    assert len(client.turn_texts) == 1
    with pytest.raises(waits.WaitError, match="different payload"):
        waits.publish_event("handoff", "run1-release", "changed", THREAD)
    waits.register(THREAD, "event", {"name": "handoff", "event_id": "run2-release"})
    waits.tick(lambda: client)
    assert len(client.turn_texts) == 1
    waits.publish_event("handoff", "run2-release", "ready", THREAD)
    waits.tick(lambda: client)
    assert len(client.turn_texts) == 2


def test_unknown_admission_with_missing_history_never_resends():
    waits.register(THREAD, "timer", {}, timeout=1, now=1000)
    client = FakeClient(fail_turn=True)
    waits.tick(lambda: client, now=1002)
    client.fail_turn = False
    for now in (1100, 1500, 2000):
        waits.tick(lambda: client, now=now)
    assert client.methods().count("turn/start") == 1
    assert waits.load()[0]["delivery"]["reconciliation"] == "unknown_hold"


def test_recipient_receipts_require_target_and_admission():
    wait = waits.register(THREAD, "timer", {}, timeout=1, now=1000)
    with pytest.raises(waits.WaitError, match="admission"):
        waits.record_recipient(wait["id"], THREAD)
    waits.tick(lambda: FakeClient(goal=None), now=1002)
    with pytest.raises(waits.WaitError, match="target"):
        waits.record_recipient(wait["id"], "other-thread")
    current = waits.record_recipient(wait["id"], THREAD, action="waiting", reason="owner pending")
    assert current["recipient_read"]["thread"] == THREAD
    assert current["disposition"]["action"] == "waiting"
    assert waits.record_recipient(wait["id"], THREAD, action="waiting", reason="owner pending")
    with pytest.raises(waits.WaitError, match="already recorded"):
        waits.record_recipient(wait["id"], THREAD, action="complete")


def test_user_pause_after_wait_is_preserved_and_legacy_pause_is_not_ownership():
    wait = waits.register(THREAD, "timer", {}, timeout=1, now=1000)
    waits.update(
        wait["id"],
        lambda w: w["goal"].update(
            paused_by_wait=True, pause_snapshot={"status": "paused", "updatedAt": 10}
        ),
    )
    client = FakeClient(goal="paused")
    waits.tick(lambda: client, now=1002)
    assert "thread/goal/set" not in client.methods()
    assert not waits.pause_owned({"goal": {"paused_by_wait": True}}, {"status": "paused"})


def test_keyed_event_can_rearm_after_deadline_but_not_after_event():
    spec = {"name": "handoff", "event_id": "run1"}
    first = waits.register(THREAD, "event", spec, timeout=1, now=1000)
    waits.tick(lambda: FakeClient(goal=None), now=1002)
    second = waits.register(THREAD, "event", spec, timeout=600, now=1003)
    assert second["id"] != first["id"]
    waits.publish_event("handoff", "run1", "ready", THREAD)
    waits.tick(lambda: FakeClient(goal=None))
    assert waits.register(THREAD, "event", spec)["id"] == second["id"]


def test_exact_event_target_precedes_broadcast_and_expired_key_is_reusable():
    waits.publish_event("handoff", "run1", "broadcast", now=1000)
    waits.publish_event("handoff", "run1", "exact", THREAD, now=1000)
    waits.register(THREAD, "event", {"name": "handoff", "event_id": "run1"}, now=1001)
    waits.consume_events(now=1002)
    assert waits.load()[0]["outcome"]["evidence"] == "exact"
    later = 1000 + waits.EVENT_RETENTION_SECONDS + 1
    assert not waits.publish_event("handoff", "run1", "fresh", THREAD, now=later)["duplicate"]


def test_durable_goal_guard_rejects_user_edits_and_truncated_logs(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text("")
    guard = {"path": str(path), "offset": 0, "inode": path.stat().st_ino}
    goal = {"status": "paused", "objective": "fixture", "createdAt": 10, "tokensUsed": 0}
    path.write_text(
        json.dumps({"type": "event_msg", "payload": {"type": "thread_goal_updated", "goal": goal}})
        + "\n"
    )
    snapshot = waits.capture_pause_guard(guard, goal)
    assert snapshot and waits.goal_log_unchanged(snapshot, {**goal, "tokensUsed": 50})
    with path.open("a") as f:
        f.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "internal_chat_message_metadata_passthrough": {
                            "content_item_kinds": ["user.goal"]
                        }
                    },
                }
            )
            + "\n"
        )
    assert not waits.goal_log_unchanged(snapshot, goal)
    path.write_text("{unfinished")
    assert not waits.goal_log_unchanged(snapshot, goal)


def test_other_pending_wait_prevents_automatic_goal_resume(monkeypatch):
    monkeypatch.setattr(waits, "goal_log_unchanged", lambda *args: True)
    first = waits.register(THREAD, "timer", {}, timeout=1, now=1000)
    second = waits.register(THREAD, "timer", {}, timeout=600, now=1000)
    for w in (first, second):
        waits.update(
            w["id"],
            lambda current: current["goal"].update(
                paused_by_wait=True,
                pause_snapshot={"status": "paused"},
                mutation_guard={"fixture": True},
            ),
        )
    client = FakeClient(goal="paused")
    waits.tick(lambda: client, now=1002)
    assert client.goal == "paused" and "thread/goal/set" not in client.methods()
