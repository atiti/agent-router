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


def test_turn_end_without_new_evidence_does_not_announce():
    assert voice.next_announcements({"work": "working"}, {"work": agent(state="idle")}) == {}


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
    method, args = target.request.call_args.args
    assert method == "turn/steer" and args["expectedTurnId"] == "next-turn"
    assert args["input"][0]["text"].startswith("Check CI.\n\n")
    assert current.last_instruction["state"] == "steer_accepted"
    assert current.last_instruction["turn_id"] == "next-turn"


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

    def send(team, member, message_id, text, *, expected_thread, voice_reply_marker):
        assert voice_reply_marker in message_id
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
    assert voice.team_receipt("team", "voice-abc", {
        "state": "queued", "coordination_event": {"state": "acked"},
    })["state"] == "acknowledged"


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


def test_updates_from_two_session_sources_are_not_replayed(monkeypatch):
    from unittest.mock import Mock

    shared = agent("shared", state="working")
    shared["title"] = "Shared session"
    core = agent("herdr", state="working")
    core["title"] = "Herdr worker"
    current = pilot(shared, core)
    clock = Mock(return_value=100)
    monkeypatch.setattr(voice.time, "monotonic", clock)
    monkeypatch.setattr(voice.time, "time", lambda: 1000)
    client = Mock()
    started = {"method": "thread/realtime/started", "params": {"threadId": current.thread_id}}
    current.event(client, started)
    shared["activity"]["message"] = {"at": 999, "text": "The Linux build passed."}
    core["activity"]["message"] = {"at": 1000, "text": "Waiting for the database backup."}
    clock.return_value = 113
    current.update(client)
    spoken = client.request.call_args.args[1]["text"]
    assert "The Linux build passed." in spoken
    assert "Waiting for the database backup." in spoken
    client.reset_mock()
    current.event(client, started)
    clock.return_value = 130
    current.update(client)
    client.request.assert_not_called()


def test_recalled_context_cannot_trigger_a_command_before_fresh_speech(monkeypatch):
    import json

    current = pilot(agent())
    client = Mock()
    dispatched = Mock(return_value={"state": "queued"})
    monkeypatch.setattr(current, "dispatch", dispatched)
    command = {"id": 1, "method": "item/tool/call",
               "params": {"threadId": current.thread_id, "tool": "message_session"}}
    current.event(client, {"method": "thread/realtime/started",
                           "params": {"threadId": current.thread_id}})
    current.event(client, command)
    dispatched.assert_not_called()
    assert json.loads(client.connection.send.call_args.args[0])["result"]["success"] is False
    current.event(client, {"method": "thread/realtime/transcript/done",
                           "params": {"threadId": current.thread_id, "role": "user",
                                      "text": "Ask the worker to check CI."}})
    current.event(client, command)
    dispatched.assert_called_once()


def test_linked_reply_requires_exact_thread_marker_and_final_message(tmp_path, monkeypatch):
    import json
    from uuid import uuid4

    from agentroute import voice_evidence

    target, other = str(uuid4()), str(uuid4())
    folder = tmp_path / "sessions"
    folder.mkdir()
    path = folder / "session.jsonl"
    monkeypatch.setattr(voice_evidence, "codex_home", lambda: tmp_path)
    monkeypatch.setattr(voice_evidence, "_rows", lambda *args: [{"rollout_path": str(path)}])

    def write(phase="final_answer", phrase="[voice-reply token] Rollback finished.", at=101):
        path.write_text("\n".join(map(json.dumps, [
            {"type": "session_meta", "payload": {"id": target}},
            {"type": "event_msg", "timestamp": at, "payload": {"type": "item_completed",
             "turn_id": "turn-1", "item": {"id": "message-1", "type": "AgentMessage",
             "phase": phase, "content": [{"type": "output_text", "text": phrase}]}}},
        ])) + "\n")

    write()
    assert voice_evidence.linked_reply(target, "token", 100) == {
        "text": "Rollback finished.", "at": 101, "thread_id": target,
        "turn_id": "turn-1", "item_id": "message-1",
    }
    assert voice_evidence.linked_reply(target, "token", 100, expected_turn="turn-2") is None
    assert voice_evidence.linked_reply(target, "token", 100, expected_turn="turn-1")
    assert voice_evidence.linked_reply(other, "token", 100) is None
    assert voice_evidence.linked_reply(target, "other-token", 100) is None
    write(phase="commentary")
    assert voice_evidence.linked_reply(target, "token", 100) is None
    write(at=99)
    assert voice_evidence.linked_reply(target, "token", 100) is None
    write(phrase="I saw someone use [voice-reply token] as a marker.")
    assert voice_evidence.linked_reply(target, "token", 100) is None


    legacy = [
        {"type": "session_meta", "payload": {"id": target}},
        {"type": "response_item", "timestamp": 101, "payload": {
            "type": "message", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": "[voice-reply token] Build passed."}],
        }, "metadata": {"retained_source": {"id": {
            "turn_id": "legacy-turn", "message_id": "legacy-message",
        }}}},
    ]
    path.write_text("\n".join(map(json.dumps, legacy)) + "\n")
    assert voice_evidence.linked_reply(target, "token", 100)["turn_id"] == "legacy-turn"


def test_shared_receipt_records_acceptance_without_inventing_read_or_reply():
    wait = {
        "thread_id": "abcd1234", "state": "delivered", "created_at": 100,
        "delivery": {"state": "delivered", "turn_id": "turn-1"},
        "recipient_read": {"thread": "another-thread", "at": 110},
        "spec": {}, "outcome": {"evidence": "Check CI."},
    }
    receipt = voice.shared_receipt(wait, {"recipient_read": {"at": 110}})
    assert receipt["receipt_state"] == "accepted_by_codex"
    assert receipt["recipient_read"] is None
    assert receipt["verified_reply"] is None
    assert receipt["turn_id"] == "turn-1"
    wait["recipient_read"]["thread"] = wait["thread_id"]
    assert voice.shared_receipt(wait, {"recipient_read": {"at": 110}})["receipt_state"] == "read"


def test_update_uses_substantive_evidence_and_groups_project(monkeypatch):
    first = agent("core", title="Worker")
    second = agent("shared", title="Supervisor")
    current = pilot(first, second)
    client = Mock()
    clock = Mock(return_value=100)
    monkeypatch.setattr(voice.time, "monotonic", clock)
    monkeypatch.setattr(voice.time, "time", lambda: 1000)
    current.event(client, {"method": "thread/realtime/started", "params": {"threadId": "voice"}})
    first["activity"]["message"] = {"at": 999, "text": "Recovered the four missing items."}
    second["activity"]["message"] = {"at": 998, "text": "The Linux build failed on lint."}
    clock.return_value = 125
    current.update(client)
    spoken = client.request.call_args.args[1]["text"]
    assert spoken.startswith("On work.")
    assert "Recovered the four missing items" in spoken and "Linux build failed" in spoken
    result = current.dispatch({"namespace": voice.NAMESPACE, "tool": "update_evidence",
                               "arguments": {"query": "Supervisor"}})
    assert [item["thread_id"] for item in result["updates"]] == ["shared"]
    assert result["updates"][0]["reported_at"] == 998
    first["activity"]["message"] = {"at": 1000, "text": "I'll check and get back to you."}
    second["activity"]["message"]["at"] = 1000
    clock.return_value = 143
    current.update(client)
    assert client.request.call_count == 1
