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
        "work": "Release has a new update.",
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


def test_message_uses_durable_queue_and_original_receipt(monkeypatch, tmp_path):
    receiver = agent("abcd1234")
    current = pilot(receiver)
    monkeypatch.setattr(voice.dashboard_packet_routes, "core_member", lambda _: None)
    monkeypatch.setattr(voice.dashboard_actions, "preflight", lambda _: None)
    monkeypatch.setattr(voice, "loaded_ids", lambda _: {"abcd1234"})
    monkeypatch.setattr(voice.waits, "waits_dir", lambda: tmp_path)
    monkeypatch.setattr(voice.waits, "ensure_daemon", lambda: True)
    rpc = Mock()
    rpc.return_value.__enter__ = Mock(return_value=Mock())
    rpc.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(voice, "RpcClient", rpc)
    first = current.message("test-call", receiver, "Check CI.")
    retry = current.message("test-call", receiver, "Check CI.")
    assert first == retry
    assert first["state"] == "fired"
    assert len(voice.waits.load()) == 1


def test_local_voice_page_requires_token_and_same_origin(tmp_path):
    import http.client
    import json
    import threading

    from agentroute.voice_server import VoiceServer

    with VoiceServer(tmp_path) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            client.request("GET", "/")
            page = client.getresponse()
            assert page.status == 200
            assert b"Start talking" in page.read()
            for headers in ({}, {"X-AgentRoute-Token": server.token,
                                 "Origin": "http://other.example"}):
                client.request("POST", "/api/voice", "{}", headers)
                failed = client.getresponse()
                assert failed.status == 403
                failed.read()
            client.request(
                "POST", "/api/voice", json.dumps({"action": "status", "session": "missing"}),
                {"Content-Type": "application/json", "Origin": origin,
                 "X-AgentRoute-Token": server.token},
            )
            missing = client.getresponse()
            assert missing.status == 410
            missing.read()
        finally:
            client.close()
            server.shutdown()
            thread.join()


def test_voice_transcript_persists_completed_dialogue(monkeypatch, tmp_path):
    import json
    import threading
    from concurrent.futures import Future

    from agentroute import voice_server

    monkeypatch.setattr(voice_server, "agentroute_home", lambda: tmp_path)
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    session = voice_server.VoiceSession(tmp_path, "offer")
    session.answer = Future()
    session.answer.set_result("answer")
    session.last_seen = voice_server.time.monotonic()
    pilot = Mock()
    pilot.thread_id = "voice"
    pilot.update = Mock()
    client = Mock()
    events = [
        {"method": "thread/realtime/transcript/done",
         "params": {"threadId": "other", "role": "user", "text": "ignore"}},
        {"method": "thread/realtime/transcript/done",
         "params": {"threadId": "voice", "role": "user", "text": "How is it going?"}},
        {"method": "thread/realtime/transcript/done",
         "params": {"threadId": "voice", "role": "assistant", "text": "Still working."}},
    ]

    def receive(_client):
        if events:
            return events.pop(0)
        session.stop.set()
        return None

    monkeypatch.setattr(session, "receive", receive)
    session.pump(client, pilot)
    records = [json.loads(line) for line in session.transcript.read_text().splitlines()]
    assert [(r["role"], r["text"]) for r in records] == [
        ("user", "How is it going?"), ("assistant", "Still working."),
    ]
    assert session.transcript.stat().st_mode & 0o777 == 0o600
    assert session.transcript.parent.stat().st_mode & 0o777 == 0o700


def test_voice_routes_herdr_owner_and_reports_receipts(monkeypatch):
    from agentroute import herdr_team

    current = pilot(agent("abcd1234"))
    calls = []
    monkeypatch.setattr(voice.dashboard_packet_routes, "core_member",
                        lambda _: ("team", "worker", "workspace"))

    def send(team, member, message_id, text, *, expected_thread):
        calls.append((team, member, message_id, text, expected_thread))
        return {"state": "submitted", "target_thread": expected_thread}

    monkeypatch.setattr(herdr_team, "submit_message", send)
    receipt = current.message("tool-call", agent("abcd1234"), "Check CI.")
    assert receipt["state"] == "entered_in_terminal"
    assert calls[0][0:2] == ("team", "worker")
    assert calls[0][-1] == "abcd1234"
    assert current.message("tool-call", agent("abcd1234"), "Check CI.") == receipt


def test_voice_team_receipt_does_not_overclaim():
    queued = voice.team_receipt("team", "voice-abc", {
        "state": "queued", "target_thread": "target",
        "coordination_event": {"delivery": {"state": "pending"}},
    })
    assert queued["state"] == "queued"
    unknown = voice.team_receipt("team", "voice-abc", {
        "state": "unknown", "target_thread": "target",
    })
    assert unknown["state"] == "unconfirmed"


def test_preferences_and_history_survive_restart(tmp_path, monkeypatch):
    import json

    from agentroute import voice_preferences as prefs

    monkeypatch.setattr(prefs, "agentroute_home", lambda: tmp_path)
    prefs.save_preference("updates", "blockers")
    prefs.save_preference("detail", "detailed")
    preferences = prefs.load_preferences()
    assert preferences["updates"] == "blockers" and preferences["detail"] == "detailed"
    assert prefs.preferences_path().stat().st_mode & 0o777 == 0o600
    earlier = tmp_path / "20261125.jsonl"
    earlier.write_text(json.dumps({"role": "user", "text": "Remember the test phrase."}) + "\n")
    current = tmp_path / "20261126.jsonl"
    current.write_text(json.dumps({"role": "user", "text": "Ignore this file."}) + "\n")
    assert prefs.recent_dialogue(current) == [{"role": "user", "text": "Remember the test phrase."}]
