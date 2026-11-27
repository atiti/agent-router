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


@pytest.mark.parametrize("https_origin", [None, "https://mac.example.ts.net:8443"])
def test_voice_page_requires_token_and_same_origin(tmp_path, https_origin):
    import http.client
    import json
    import threading

    from agentroute.voice_server import VoiceServer

    with VoiceServer(tmp_path, https_origin=https_origin) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        origin = https_origin or server.local_origin
        host = origin.split("://", 1)[1]
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            client.request("GET", "/", headers={"Host": host})
            page = client.getresponse()
            assert page.status == 200
            assert b"Start talking" in page.read()
            for headers in (
                {"Host": host, "Origin": origin},
                {"Host": host, "X-AgentRoute-Token": server.token,
                 "Origin": "http://other.example"},
                {"Host": "other.example", "X-AgentRoute-Token": server.token,
                 "Origin": origin, "X-Forwarded-Host": host, "X-Forwarded-Proto": "https"},
                {"Host": host, "X-AgentRoute-Token": server.token, "Origin": origin,
                 "Sec-Fetch-Site": "same-site"},
            ):
                client.request("POST", "/api/voice", "{}", headers)
                failed = client.getresponse()
                assert failed.status == 403
                failed.read()
            client.request(
                "POST", "/api/voice", json.dumps({"action": "status", "session": "missing"}),
                {"Content-Type": "application/json", "Origin": origin, "Host": host,
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


@pytest.fixture
def reply_rollout(tmp_path, monkeypatch):
    import json
    from uuid import uuid4

    from agentroute import voice_evidence

    target = str(uuid4())
    folder = tmp_path / "sessions"
    folder.mkdir()
    path = folder / "session.jsonl"
    monkeypatch.setattr(voice_evidence, "codex_home", lambda: tmp_path)
    monkeypatch.setattr(voice_evidence, "_rows", lambda *args: [{"rollout_path": str(path)}])

    def write(*events):
        records = [
            {"type": "session_meta", "payload": {"id": target}},
            *({"type": "event_msg", "timestamp": event.get("at", 101), "payload": event}
              for event in events),
        ]
        path.write_text("".join(json.dumps(record) + "\n" for record in records))

    def read(turn="turn-1", *, thread=target, marker="token", input_prefix=None):
        return voice_evidence.reply_evidence(
            thread, marker, 100, expected_turn=turn, input_prefix=input_prefix,
        )

    return target, write, read


@pytest.mark.parametrize("phase_fields", [
    {}, {"phase": None}, {"phase": "final_answer"}, {"phase": "runtime-variant"},
])
def test_reply_uses_receipt_linked_completion_independent_of_phase(reply_rollout, phase_fields):
    target, write, read = reply_rollout
    answer = "Rollback finished. " + "All checks passed. " * 70
    tagged = f"[voice-reply token] {answer}"
    write(
        {"type": "item_completed", "turn_id": "turn-1", "item": {
            "type": "AgentMessage", **phase_fields,
            "content": [{"type": "output_text", "text": tagged}],
        }},
        {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": tagged},
    )
    assert read() == {
        "reply_state": "replied", "reply_turn_id": "turn-1",
        "verified_reply": {
            "text": answer.rstrip(), "at": 101, "thread_id": target,
            "turn_id": "turn-1", "truncated": False,
        },
    }
    assert read("turn-2")["reply_state"] == "output_unconfirmed"
    assert read(thread="abcd1234")["reply_state"] == "output_unconfirmed"
    assert read(marker="other-token")["reply_state"] == "completed_without_reply"


def test_status_never_attaches_progress_or_unrelated_updates(reply_rollout):
    _, write, read = reply_rollout
    wrong = "[voice-reply token] Progress that must not be attached."
    progress = {"type": "item_completed", "turn_id": "turn-1", "item": {
        "type": "AgentMessage", "phase": "commentary",
        "content": [{"type": "output_text", "text": wrong}],
    }}
    unrelated = {"type": "task_complete", "turn_id": "another-turn", "last_agent_message": wrong}
    write(progress, unrelated)
    assert read()["reply_state"] == "pending"
    assert read()["verified_reply"] is None

    write(progress, unrelated, {
        "type": "task_complete", "turn_id": "turn-1", "last_agent_message": "An untagged final.",
    })
    assert read()["reply_state"] == "completed_without_reply"
    assert read()["verified_reply"] is None

    write(progress, unrelated, {
        "type": "task_complete", "turn_id": "turn-1", "last_agent_message": wrong,
    })
    assert read()["reply_state"] == "output_unconfirmed"
    assert read()["verified_reply"] is None


@pytest.mark.parametrize(("terminal", "state"), [
    ({"type": "task_complete"}, "output_unconfirmed"),
    ({"type": "task_complete", "last_agent_message": {}}, "output_unconfirmed"),
    ({"type": "task_complete", "last_agent_message": None}, "completed_without_reply"),
    ({"type": "task_complete", "error": {"message": "failed"}}, "failed"),
    ({"type": "turn_aborted"}, "interrupted"),
])
def test_reply_reports_terminal_without_verified_output(reply_rollout, terminal, state):
    _, write, read = reply_rollout
    write({"turn_id": "turn-1", **terminal})
    assert read()["reply_state"] == state
    assert read()["verified_reply"] is None


def test_conflicting_completions_leave_reply_unconfirmed(reply_rollout):
    _, write, read = reply_rollout
    first = {"type": "task_complete", "turn_id": "turn-1",
             "last_agent_message": "[voice-reply token] First."}
    conflict = {**first, "last_agent_message": "[voice-reply token] Second."}
    write(first, conflict, first)
    assert read()["reply_state"] == "output_unconfirmed"
    assert read()["verified_reply"] is None


def test_missing_receipt_turn_requires_unique_exact_input(reply_rollout):
    _, write, read = reply_rollout
    prefix = "[agentroute wait-event w-exact]"
    user = {"type": "item_completed", "turn_id": "turn-1", "item": {
        "type": "UserMessage", "content": [{"text": f"{prefix} Check the build."}],
    }}
    final = {"type": "task_complete", "turn_id": "turn-1",
             "last_agent_message": "[voice-reply token] Build passed."}
    write({**user, "at": 99}, final)
    assert read(None, input_prefix=prefix)["reply_state"] == "turn_unconfirmed"
    write(user, final)
    assert read(None, input_prefix=prefix)["verified_reply"]["text"] == "Build passed."
    assert read(None, input_prefix="[agentroute wait-event w-ex]")["verified_reply"] is None
    write(user, {**user, "turn_id": "turn-2"}, final)
    assert read(None, input_prefix=prefix)["reply_state"] == "turn_unconfirmed"


def test_shared_receipt_records_acceptance_without_inventing_read_or_reply():
    wait = {
        "id": "w-test", "thread_id": "abcd1234", "state": "delivered", "created_at": 100,
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


def test_stable_access_key_survives_restarts_and_rejects_insecure_file(tmp_path):
    from agentroute.voice_server import VoiceServer

    key = tmp_path / "voice-access.key"
    with VoiceServer(tmp_path, access_key_file=key) as first:
        token = first.token
        assert key.stat().st_mode & 0o777 == 0o600
    with VoiceServer(tmp_path, access_key_file=key) as second:
        assert second.token == token
    with VoiceServer(tmp_path) as default:
        assert default.token != token
    key.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        VoiceServer(tmp_path, access_key_file=key)


def test_voice_access_key_refuses_symlink_and_invalid_content(tmp_path):
    from agentroute.voice_server import voice_access_key

    key = tmp_path / "voice-access.key"
    key.write_text("not-a-valid-key\n")
    key.chmod(0o600)
    with pytest.raises(ValueError, match="URL-safe"):
        voice_access_key(key)
    alias = tmp_path / "alias"
    alias.symlink_to(key)
    with pytest.raises(OSError):
        voice_access_key(alias)
