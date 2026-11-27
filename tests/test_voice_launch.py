"""Verify Voice launch retries across App Server, wait ledger and Herdr boundaries."""

from contextlib import nullcontext
from unittest.mock import Mock
from uuid import uuid4

import pytest

from agentroute import voice, voice_launch
from agentroute.shared_server import SharedServerError


@pytest.fixture
def launch(tmp_path, monkeypatch):
    target = str(uuid4())
    rpc = Mock()
    rpc.request.return_value = {"thread": {"id": target, "cwd": str(tmp_path)}}
    monkeypatch.setattr(voice_launch, "RpcClient", lambda *args, **kw: nullcontext(rpc))
    monkeypatch.setattr(voice_launch, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(voice_launch.waits, "waits_dir", lambda: tmp_path / "waits")
    monkeypatch.setattr(voice_launch.waits, "ensure_daemon", Mock())
    monkeypatch.setattr(voice_launch.waits, "daemon_running", lambda: True)
    monkeypatch.setattr(voice_launch.chats, "operation_lock", nullcontext)
    remember = Mock()
    monkeypatch.setattr(voice_launch.chats, "remember_chat_namespace", remember)
    monkeypatch.setattr(voice, "canonical_namespace",
                        lambda name: "Private" if name == "private" else None)
    panes = Mock(return_value=[])
    open_thread = Mock(return_value="workspace:pane")
    monkeypatch.setattr(voice_launch.herdr, "panes", panes)
    monkeypatch.setattr(voice_launch.herdr, "open_thread", open_thread)
    pilot = voice.VoicePilot(Mock(), cwd=tmp_path)
    pilot.thread_id = str(uuid4())
    request = {
        "namespace": voice.NAMESPACE, "tool": "create_session", "callId": "test-launch",
        "arguments": {"namespace": "private", "directory": "", "title": "Dash cam research",
                      "message": "Compare affordable dash cams for two cars."},
    }
    return pilot, request, target, rpc, panes, open_thread, remember


def test_offline_herdr_keeps_delivery_and_retries_on_same_thread(launch):
    pilot, request, target, rpc, panes, open_thread, remember = launch
    panes.side_effect = SharedServerError("offline")
    first = pilot.dispatch(request)
    assert first["creation_state"] == "created"
    assert first["receipt_state"] == "queued"
    assert first["herdr_state"] == "offline"
    assert first["recipient"] == target
    remember.assert_called_once_with(target, "Private")
    assert pilot.dispatch(request)["instruction_receipt_id"] == first["instruction_receipt_id"]
    panes.side_effect = None
    retry = pilot.dispatch({
        "namespace": voice.NAMESPACE, "tool": "retry_created_session",
        "arguments": {"receipt_id": first["id"]},
    })
    assert retry["recipient"] == target
    assert retry["herdr_pane"] == "workspace:pane"
    assert open_thread.call_args.args[0]["id"] == target
    assert open_thread.call_args.kwargs["workspace_label"] == "Private"
    assert open_thread.call_args.kwargs["verify_native"] is True
    assert [c.args[0] for c in rpc.request.call_args_list].count("thread/start") == 1
    assert len(voice_launch.waits.load()) == 1
    request["arguments"]["message"] = "A different instruction."
    with pytest.raises(ValueError, match="different launch"):
        pilot.dispatch(request)


def test_unknown_creation_never_starts_another_thread(launch):
    pilot, request, _, rpc, _, open_thread, _ = launch
    rpc.request.side_effect = TimeoutError
    first = pilot.dispatch(request)
    assert first["creation_state"] == "unconfirmed"
    assert pilot.dispatch(request) == first
    assert voice_launch.status(first["id"].split(":")[1], retry=True) == first
    rpc.request.assert_called_once()
    open_thread.assert_not_called()
    assert not voice_launch.waits.load()


def test_unknown_pane_is_reconciled_by_native_thread_id(launch):
    pilot, request, target, _, panes, open_thread, _ = launch
    open_thread.side_effect = TimeoutError
    first = pilot.dispatch(request)
    assert first["herdr_state"] == "unconfirmed"
    assert pilot.dispatch(request)["herdr_state"] == "unconfirmed"
    panes.return_value = [{
        "pane_id": "verified-pane",
        "agent_session": {"source": "herdr:codex", "kind": "id", "value": target},
    }]
    assert pilot.check_receipt(first["id"])["herdr_pane"] == "verified-pane"
    open_thread.assert_called_once()
