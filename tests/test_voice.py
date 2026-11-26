"""Regression tests for the data and effect boundaries of the voice conversation."""

from unittest.mock import Mock

import pytest

from agentroute import voice


def agent(id="work", state="working", title="Release"):
    return {
        "id": id, "state": state, "title": title, "cwd": "/work", "packets": [],
        "activity": {"last_turn": {"outcome": "completed"}},
    }


def pilot(*agents):
    collector = Mock()
    collector.snapshot.return_value = {
        "agents": agents, "generated_at": 1000, "sources": {"shared_owner": True},
    }
    result = voice.VoicePilot(collector)
    result.thread_id = "voice"
    return result


def test_announces_only_new_work_and_waits_for_pause(monkeypatch):
    current = pilot(agent(), agent("old", "done"))
    clock = Mock(return_value=100)
    monkeypatch.setattr(voice.time, "monotonic", clock)
    client = Mock()
    current.event(client, {"method": "thread/realtime/started", "params": {"threadId": "voice"}})
    current.collector.snapshot.return_value["agents"] = (
        agent(state="needs_input"), agent("old", "done"),
    )
    clock.return_value = 115
    current.event(client, {"method": "thread/realtime/transcript/delta",
                           "params": {"threadId": "voice"}})
    current.update(client)
    client.request.assert_not_called()
    clock.return_value = 119
    current.update(client)
    client.request.assert_called_once_with(
        "thread/realtime/appendSpeech", {"threadId": "voice", "text": "Release needs your input."}
    )
    clock.return_value = 135
    current.update(client)
    assert client.request.call_count == 1


def test_announces_turn_end_without_claiming_task_done():
    assert voice.next_announcements({"work": "working"}, {"work": agent(state="idle")}) == {
        "work": "Release finished its latest turn.",
    }


def test_scopes_tools_to_conductor_and_current_target(monkeypatch):
    current = pilot(agent(), agent("voice"))
    client = Mock()
    params = {"threadId": "another", "namespace": voice.NAMESPACE,
              "tool": "sessions", "arguments": {"query": ""}}
    current.event(client, {"id": 1, "method": "item/tool/call", "params": params})
    client.connection.send.assert_not_called()
    params["threadId"] = "voice"
    result = current.dispatch(params)
    assert [session["id"] for session in result["sessions"]] == ["work"]

    monkeypatch.setattr(voice.dashboard_packet_routes, "core_member", lambda thread_id: None)
    monkeypatch.setattr(voice, "active_turn", lambda *_: "next-turn")
    target = Mock()
    rpc = Mock()
    rpc.return_value.__enter__ = Mock(return_value=target)
    rpc.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(voice, "RpcClient", rpc)
    params.update(callId="stop-1", tool="interrupt_session",
                  arguments={"thread_id": "work", "turn_id": "old-turn"})
    with pytest.raises(ValueError, match="no longer active"):
        current.dispatch(params)
    target.request.assert_not_called()
    params["arguments"]["turn_id"] = "next-turn"
    current.dispatch(params)
    target.request.assert_called_once_with("turn/interrupt",
                                           {"threadId": "work", "turnId": "next-turn"})

    assert current.dispatch(params)["state"] == "interrupt_accepted"
    assert target.request.call_count == 1

    params.update(callId="steer-1", tool="steer_session",
                  arguments={"thread_id": "work", "turn_id": "next-turn", "message": "Check CI."})
    assert current.dispatch(params)["state"] == "steer_accepted"
    target.request.assert_called_with("turn/steer", {
        "threadId": "work", "expectedTurnId": "next-turn",
        "input": [{"type": "text", "text": "Check CI."}],
    })
