import json
from unittest.mock import Mock
from uuid import uuid4

import pytest
import typer
from typer.testing import CliRunner

from agentroute import dashboard_actions as actions
from agentroute import dashboard_decisions as decisions

KEY = ("private", "fixture", "release")
OWNER = "01a20744-50ee-71a2-a6a3-3e26905fc41b"
PROPOSAL = "Install release abc123 on staging fixture, with the reviewed rollback."


@pytest.fixture
def decision_setup(monkeypatch, tmp_path):
    item = {"revision": 5, "status": "blocked", "assignment": {"session": OWNER},
            "next_step": {"id": "await-fixture", "state": "awaiting_user",
                          "action": "Wait for scoped decision",
                          "wait": {"owner": "Attila", "reason": "Waiting",
                                   "resume_when": "Explicit approval",
                                   "question_ref": f"2026-11-25 request: {PROPOSAL}"}}}
    monkeypatch.setattr(actions, "read_item", lambda key: item)
    monkeypatch.setattr(decisions.routes, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(decisions, "native_owner", lambda target: False)
    monkeypatch.setattr(decisions.sys, "platform", "darwin")
    confirm = Mock()
    monkeypatch.setattr(decisions, "confirm_operator", confirm)
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock()

    def rpc(method, data):
        if method == "thread/read":
            return {"thread": {"id": OWNER, "status": {"type": "idle"}}}
        assert method == "turn/start"
        return {"turn": {"id": "test-turn"}}

    client.request.side_effect = rpc
    monkeypatch.setattr(decisions, "RpcClient", lambda *args, **kwargs: client)
    scope = decisions.preview(KEY, ["Attila"])
    payload = dict(zip(("namespace", "packet", "item"), KEY, strict=True))
    payload.update(revision=5, slot="step", decision="approve", request_id=str(uuid4()),
                   scope_digest=scope["scope_digest"])
    return item, payload, confirm, client


@pytest.mark.parametrize("choice", ["approve", "deny"])
def test_confirmed_decision_sends_exact_scope_once(decision_setup, choice):
    item, payload, confirm, client = decision_setup
    payload["decision"] = choice
    got = decisions.submit(payload, ["Attila"])
    assert got["state"] == "delivered"
    assert got["decision"] == choice
    assert decisions.submit(payload, ["Attila"]) == got
    assert decisions.preview(KEY, ["Attila"])["recorded"] == got
    assert confirm.call_count == 1
    sent = client.request.call_args.args[1]["input"][0]["text"]
    assert sent.startswith(f"I {choice} the exact")
    assert PROPOSAL in sent and payload["scope_digest"] in sent
    assert "step await-fixture" in sent
    assert f"`agentroute maintenance receipt {' '.join(KEY)} {payload['request_id']}`" in sent
    assert item["status"] == "blocked"
    assert decisions.history(KEY) == [got]
    with pytest.raises(actions.ActionError, match="already recorded"):
        decisions.submit({**payload, "request_id": str(uuid4())}, ["Attila"])


def test_stale_or_cancelled_decision_sends_nothing(decision_setup):
    item, payload, confirm, client = decision_setup

    def revised(*args):
        item["revision"] += 1

    confirm.side_effect = revised
    with pytest.raises(actions.ActionError, match="outcome changed"):
        decisions.submit(payload, ["Attila"])
    client.request.assert_not_called()
    assert decisions.history(KEY) == []


def test_reference_only_and_other_owner_cannot_approve(decision_setup):
    item, payload, confirm, client = decision_setup
    wait = item["next_step"]["wait"]
    wait["question_ref"] = "chat:receipt"
    preview = decisions.preview(KEY, ["Attila"])
    assert preview["eligible"] and not preview["can_decide"]
    with pytest.raises(actions.ActionError, match="written proposal"):
        decisions.submit({**payload, "scope_digest": preview["scope_digest"]}, ["Attila"])
    wait["owner"] = "Attila and service owner"
    assert not decisions.preview(KEY, ["Attila"])["eligible"]
    confirm.assert_not_called()
    client.request.assert_not_called()


def test_comment_cannot_submit_native_approval_and_free_text_cannot_ride_approve(
    decision_setup, monkeypatch,
):
    _, payload, confirm, client = decision_setup
    submit = Mock(return_value={"state": "fired"})
    monkeypatch.setattr(actions, "submit", submit)
    message = "I approve a different proposal"
    with pytest.raises(actions.ActionError):
        decisions.submit({**payload, "message": message}, ["Attila"])
    assert decisions.submit({**payload, "decision": "comment", "message": message},
                            ["Attila"]) == {"state": "fired"}
    assert submit.call_args.args[0]["message"] == message
    client.request.assert_not_called()
    confirm.assert_not_called()


def test_uncertain_native_delivery_is_not_replayed(decision_setup):
    _, payload, confirm, client = decision_setup
    client.request.side_effect = [
        {"thread": {"id": OWNER, "status": {"type": "idle"}}}, TimeoutError(),
    ]
    result = decisions.submit(payload, ["Attila"])
    assert result["delivery"]["state"] == "unknown"
    assert decisions.submit(payload, ["Attila"]) == result
    assert client.request.call_count == 2
    assert confirm.call_count == 1
    with pytest.raises(actions.ActionError, match="no confirmed delivery"):
        decisions.verify_receipt(KEY, payload["request_id"], OWNER)


def test_receipt_verification_reads_legacy_record_and_binds_recipient(decision_setup, monkeypatch):
    item, payload, confirm, client = decision_setup
    got = decisions.submit(payload, ["Attila"])
    receipt_id = got["id"]
    path = decisions.directory(KEY) / f"{receipt_id}.json"
    record = decisions.routes.read_record(path)
    assert record.pop("step_id") == "await-fixture"
    decisions.atomic_write(path, record)

    app = typer.Typer()
    app.callback()(lambda: None)
    decisions.register_receipt_command(app)
    runner = CliRunner()
    args = ["receipt", *KEY, receipt_id]
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    local = runner.invoke(app, args)
    assert local.exit_code == 0
    assert json.loads(local.output)["verified_as"] == "local_inspection"
    monkeypatch.setenv("CODEX_THREAD_ID", OWNER)
    recipient = runner.invoke(app, args)
    assert recipient.exit_code == 0
    verified = json.loads(recipient.output)
    assert verified["verified_as"] == "recipient"
    assert verified["step_id"] == item["next_step"]["id"]
    assert verified["scope_digest"] == payload["scope_digest"]
    assert verified["turn_id"] == "test-turn"
    assert verified["decision"] == "approve"
    monkeypatch.setenv("CODEX_THREAD_ID", str(uuid4()))
    wrong_thread = runner.invoke(app, args)
    assert wrong_thread.exit_code != 0
    assert "different session" in wrong_thread.output
    assert "test-turn" not in wrong_thread.output
    assert confirm.call_count == 1
    assert client.request.call_count == 2


def test_receipt_verification_refuses_stale_scope_and_step_id(decision_setup):
    item, payload, _, _ = decision_setup
    receipt_id = decisions.submit(payload, ["Attila"])["id"]
    item["next_step"]["id"] = "different-step"
    with pytest.raises(actions.ActionError, match="does not match"):
        decisions.verify_receipt(KEY, receipt_id, OWNER)
    item["next_step"]["id"] = "await-fixture"
    item["revision"] += 1
    with pytest.raises(actions.ActionError, match="outcome changed"):
        decisions.verify_receipt(KEY, receipt_id, OWNER)


def test_receipt_verification_refuses_a_changed_review_conversation(decision_setup, monkeypatch):
    item, payload, _, _ = decision_setup
    item["assignment"] = None
    original = {"kind": "review", "thread": OWNER, "assignment": None}
    monkeypatch.setattr(decisions.routes, "route", lambda *args: original)
    preview = decisions.preview(KEY, ["Attila"])
    got = decisions.submit({**payload, "scope_digest": preview["scope_digest"]}, ["Attila"])
    original["thread"] = str(uuid4())
    with pytest.raises(actions.ActionError, match="routes to a different session"):
        decisions.verify_receipt(KEY, got["id"], OWNER)


def test_managed_session_and_non_user_step_cannot_prompt(decision_setup, monkeypatch):
    item, payload, confirm, client = decision_setup
    monkeypatch.setattr(decisions, "native_owner", lambda target: True)
    assert not decisions.preview(KEY, ["Attila"])["can_decide"]
    with pytest.raises(actions.ActionError, match="managed session"):
        decisions.submit(payload, ["Attila"])
    item["next_step"]["state"] = "waiting_external"
    item["next_step"]["wait"]["state"] = "awaiting_user"
    assert not decisions.preview(KEY, ["Attila"])["eligible"]
    confirm.assert_not_called()
    client.request.assert_not_called()


def record_reference(monkeypatch, tmp_path, records):
    import sqlite3

    folder = tmp_path / "sessions"
    folder.mkdir()
    rollout = folder / "fixture.jsonl"
    rollout.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    with sqlite3.connect(tmp_path / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id, rollout_path, archived)")
        db.execute("INSERT INTO threads VALUES(?,?,0)", (OWNER, str(rollout)))
    monkeypatch.setattr(decisions, "codex_home", lambda: tmp_path)
    return rollout


def completed_turn(turn, text):
    return {"type": "event_msg", "payload": {
        "type": "task_complete", "turn_id": turn, "last_agent_message": text,
    }}


def test_reference_proposal_delivers_to_the_original_unassigned_thread(
    decision_setup, monkeypatch, tmp_path,
):
    item, payload, confirm, client = decision_setup
    turn = str(uuid4())
    original = "Both jobs have stopped.\n\nMay I create contacts for the held rows?"
    record_reference(monkeypatch, tmp_path, [
        {"type": "session_meta", "payload": {"id": OWNER}},
        completed_turn(str(uuid4()), "Another turn's proposal"),
        completed_turn(turn, original),
    ])
    item["assignment"] = None
    item["next_step"]["wait"]["question_ref"] = f"codex:{OWNER}/{turn}"
    preview = decisions.preview(KEY, ["Attila"])
    assert preview["proposal"] == "May I create contacts for the held rows?"
    assert preview["source_thread"] == OWNER and preview["can_decide"]
    result = decisions.submit({**payload, "scope_digest": preview["scope_digest"]}, ["Attila"])
    assert result["recipient"] == OWNER and result["state"] == "delivered"
    assert item["assignment"] is None
    assert confirm.call_args.args[1]["proposal"] == preview["proposal"]
    method, sent = client.request.call_args.args
    assert method == "turn/start" and sent["threadId"] == OWNER
    assert "May I create contacts for the held rows?" in sent["input"][0]["text"]


def test_reference_rejects_unfinished_conflicting_or_wrong_thread(decision_setup,
                                                                  monkeypatch, tmp_path):
    item, _, _, _ = decision_setup
    turn = str(uuid4())
    item["next_step"]["wait"]["question_ref"] = f"codex:{OWNER}/{turn}"
    rollout = record_reference(monkeypatch, tmp_path, [
        {"type": "session_meta", "payload": {"id": OWNER}},
        completed_turn(str(uuid4()), PROPOSAL),
    ])
    assert not decisions.preview(KEY, ["Attila"])["can_decide"]
    with rollout.open("a") as out:
        out.write(json.dumps(completed_turn(turn, PROPOSAL)) + "\n")
        out.write(json.dumps(completed_turn(turn, "Conflicting proposal")) + "\n")
    assert not decisions.preview(KEY, ["Attila"])["can_decide"]


def test_reference_change_during_confirmation_does_not_send(
    decision_setup, monkeypatch, tmp_path,
):
    item, payload, confirm, client = decision_setup
    turn = str(uuid4())
    rollout = record_reference(monkeypatch, tmp_path, [
        {"type": "session_meta", "payload": {"id": OWNER}},
        completed_turn(turn, "May I install the staging fixture?"),
    ])
    item["next_step"]["wait"]["question_ref"] = f"codex:{OWNER}/{turn}"
    preview = decisions.preview(KEY, ["Attila"])
    confirm.side_effect = lambda *args: rollout.unlink()
    with pytest.raises(actions.ActionError, match="changed"):
        decisions.submit({**payload, "scope_digest": preview["scope_digest"]}, ["Attila"])
    client.request.assert_not_called()


def test_dated_input_request_keeps_inline_proposal(decision_setup):
    item, _, _, _ = decision_setup
    item["next_step"]["wait"]["question_ref"] = (
        "chat:request_user_input_async:2026-11-11:" + PROPOSAL
    )
    result = decisions.preview(KEY, ["Attila"])
    assert result["can_decide"] and result["proposal"] == PROPOSAL
