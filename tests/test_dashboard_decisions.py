from unittest.mock import Mock
from uuid import uuid4

import pytest

from agentroute import dashboard_actions as actions
from agentroute import dashboard_decisions as decisions

KEY = ("private", "fixture", "release")
OWNER = "01a20744-50ee-71a2-a6a3-3e26905fc41b"
PROPOSAL = "Install release abc123 on staging fixture, with the reviewed rollback."


@pytest.fixture
def decision_setup(monkeypatch, tmp_path):
    item = {"revision": 5, "status": "blocked", "assignment": {"session": OWNER},
            "next_step": {"state": "awaiting_user", "action": "Wait for scoped decision",
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
