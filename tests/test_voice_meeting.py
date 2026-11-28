"""Meeting boundaries must hold even when a model names another thread or receipt."""

import http.client
import json
import subprocess
import threading
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock

import pytest

from agentroute import voice, voice_meeting, voice_server
from agentroute.voice_meeting import MeetingBinding, MeetingPilot, MeetingServer, MeetingSession

WORKER = "01a0fdde-0761-7652-9a59-851ff6c32e4e"
OTHER = "01a17d72-d57f-7721-838a-e5270d3d6328"
SDP = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"


def row(thread, state="working", text="Prepared document"):
    return {"id": thread, "title": "Private project", "cwd": "/private/project",
            "state": state, "activity": {"message": {"at": 1000, "text": text}}}


def pilot(*, allow=False):
    collector = Mock()
    collector.snapshot.return_value = {
        "agents": [row(WORKER), row(OTHER, text="Unrelated client secret")],
        "generated_at": 1000, "sources": {"private_path": "/private/state"},
    }
    current = MeetingPilot(collector, MeetingBinding("demo", WORKER, allow))
    current.thread_id = "voice"
    return current, collector


def call(name, **args):
    return {"namespace": voice.NAMESPACE, "tool": name, "arguments": args, "callId": "call-1"}


def test_inventory_progress_and_guessed_target_are_confined(monkeypatch):
    current, collector = pilot()
    result = current.dispatch(call("sessions", query=""))
    assert result["matching"] == 1
    assert result["sessions"][0]["id"] == WORKER
    assert result["sessions"][0]["title"] == "Aura work"
    assert result["sessions"][0]["project"] == ""
    assert result["sources"] == {"meeting_scope": True}
    assert "Unrelated client secret" not in json.dumps(result)
    rpc = Mock()
    monkeypatch.setattr(voice, "RpcClient", rpc)
    with pytest.raises(ValueError, match="outside"):
        current.dispatch(call("session_detail", thread_id=OTHER))
    rpc.assert_not_called()
    collector.snapshot.return_value["agents"] = [row(OTHER)]
    with pytest.raises(ValueError, match="unavailable"):
        current.dispatch(call("session_detail", thread_id=WORKER))
    rpc.assert_not_called()


def test_unrelated_progress_cannot_be_announced(monkeypatch):
    current, collector = pilot()
    clock = Mock(return_value=100)
    monkeypatch.setattr(voice.time, "monotonic", clock)
    monkeypatch.setattr(voice.time, "time", lambda: 1000)
    client = Mock()
    current.event(client, {"method": "thread/realtime/started", "params": {"threadId": "voice"}})
    collector.snapshot.return_value["agents"][1] = row(OTHER, "blocked", "New secret")
    clock.return_value = 140
    current.update(client)
    client.request.assert_not_called()
    collector.snapshot.return_value["agents"][0] = row(WORKER, "blocked", "Need reviewed dates")
    clock.return_value = 180
    current.update(client)
    assert client.request.call_count == 1
    spoken = client.request.call_args.args[1]["text"]
    assert "Aura work" in spoken and "secret" not in spoken


def test_observation_denies_actions_and_shared_settings(monkeypatch):
    current, _ = pilot()
    send, save = Mock(), Mock()
    monkeypatch.setattr(voice.VoicePilot, "message", send)
    monkeypatch.setattr(voice, "save_preference", save)
    for name, args in [
        ("message_session", {"thread_id": WORKER, "message": "Send mail"}),
        ("interrupt_session", {"thread_id": WORKER, "turn_id": "turn"}),
        ("voice_preferences", {"setting": "updates", "value": "all"}),
    ]:
        with pytest.raises(ValueError, match="not enabled"):
            current.dispatch(call(name, **args))
    send.assert_not_called()
    save.assert_not_called()


def test_request_preserves_worker_boundary_and_unknown_speaker(monkeypatch):
    current, _ = pilot(allow=True)
    receipt = {"id": "w-demo", "recipient": WORKER, "state": "queued"}
    send = Mock(return_value=receipt)
    monkeypatch.setattr(voice.VoicePilot, "message", send)
    result = current.dispatch(call("message_session", thread_id=WORKER, message="Draft the note"))
    assert result["state"] == "queued"
    assert send.call_args.args[1]["id"] == WORKER
    assert "individual speaker is not verified" in send.call_args.args[2]
    assert send.call_args.args[2].endswith("Draft the note")
    assert current.receipts == {"w-demo"}
    with pytest.raises(ValueError, match="outside"):
        current.dispatch(call("message_session", thread_id=OTHER, message="Leak another file"))
    assert send.call_count == 1


def test_receipt_lookup_rejects_guessed_or_moved_records(monkeypatch):
    current, _ = pilot()
    lookup = Mock(return_value={"id": "w-demo", "recipient": WORKER})
    monkeypatch.setattr(voice.VoicePilot, "check_receipt", lookup)
    with pytest.raises(ValueError, match="not issued"):
        current.dispatch(call("delivery_status", receipt_id="w-other"))
    lookup.assert_not_called()
    current.publish_receipt({"id": "w-demo", "recipient": WORKER}, "ignored")
    assert current.check_receipt("w-demo")["recipient"] == WORKER
    lookup.return_value = {"id": "w-demo", "recipient": OTHER}
    with pytest.raises(ValueError, match="no longer matches"):
        current.check_receipt("w-demo")


def test_meeting_voice_does_not_load_operator_history_or_preferences(monkeypatch, tmp_path):
    monkeypatch.setattr(voice_meeting, "agentroute_home", lambda: tmp_path)
    monkeypatch.setattr(threading.Thread, "start", lambda *_: None)
    history, preferences = Mock(side_effect=AssertionError), Mock(side_effect=AssertionError)
    monkeypatch.setattr(voice_server, "recent_dialogue", history)
    monkeypatch.setattr(voice_server, "load_preferences", preferences)
    session = MeetingSession(tmp_path, SDP, MeetingBinding("meeting-one", WORKER))
    current, instructions, tools = session.voice_setup()
    assert session.transcript.parent == tmp_path / "meeting-transcripts/meeting-one"
    assert session.voice_context == ""
    assert isinstance(current, MeetingPilot)
    assert "does not verify the speaker" in instructions
    assert {t["name"] for t in tools} == voice_meeting.READ_TOOLS
    history.assert_not_called()
    preferences.assert_not_called()
    client = Mock()
    client.request.return_value = {"config": {"mcp_servers": {"private": {"enabled": True}}}}
    options = session.thread_options(client, tmp_path)
    assert options["environments"] == options["runtimeWorkspaceRoots"] == []
    assert options["config"]["mcp_servers"] == {"private": {"enabled": False}}
    client.request.return_value = {}
    with pytest.raises(ValueError, match="withheld"):
        session.thread_options(client, tmp_path)


def test_candidate_configuration_disables_each_mcp_server():
    config = voice_meeting.coordinator_config({"mcp_servers": {
        "private-service": {"enabled": True}, "other.service": {"enabled": True},
    }})
    assert config["mcp_servers"] == {
        "private-service": {"enabled": False}, "other.service": {"enabled": False},
    }
    assert config["features.shell_tool"] is False
    assert config["features.apps"] is False
    with pytest.raises(ValueError, match="effective MCP"):
        voice_meeting.coordinator_config({"mcp_servers": []})


def test_http_admission_auth_and_restart_hold(monkeypatch, tmp_path):
    monkeypatch.setattr(voice_meeting, "agentroute_home", lambda: tmp_path)
    session = Mock(id="admitted", error="")
    session.answer = Future()
    session.answer.set_result("answer")
    session.instruction_receipt.return_value = None
    factory = Mock(return_value=session)
    monkeypatch.setattr(voice_meeting, "MeetingSession", factory)
    binding = MeetingBinding("one-call", WORKER)
    key = tmp_path / "access.key"

    for attempt in range(2):
        with MeetingServer(tmp_path, binding, key,
                           https_origin="https://meet.example.com") as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)

            def post(payload, connection=connection, server=server, **overrides):
                headers = {"Host": "meet.example.com", "Origin": "https://meet.example.com",
                           "Content-Type": "application/json", "X-AgentRoute-Token": server.token}
                headers.update(overrides)
                connection.request("POST", "/api/voice", json.dumps(payload), headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())

            try:
                assert post({"action": "start", "sdp": SDP},
                            **{"X-AgentRoute-Token": "wrong"})[0] == 403
                assert post({"action": "start", "sdp": SDP},
                            **{"Origin": "https://elsewhere.example"})[0] == 403
                status, body = post({"action": "start", "sdp": SDP})
                assert status == (200 if attempt == 0 else 409)
                assert post({"action": "start", "sdp": SDP})[0] == 409
                if attempt == 0:
                    assert body == {"session": "admitted", "sdp": "answer"}
                    assert post({"action": "status", "session": "admitted"})[0] == 200
                    assert post({"action": "stop", "session": "admitted"})[0] == 200
                    session.stop.set.assert_called_once()
                else:
                    assert post({"action": "status", "session": "admitted"})[0] == 410
            finally:
                connection.close()
                server.shutdown()
                worker.join()
    factory.assert_called_once_with(tmp_path, SDP, binding)
    assert (tmp_path / "meeting-admissions/one-call.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("value", ["http://example.com", "https://a.example/path",
                                  "https://user:pass@example.com", "https://a.example/?token=x"])
def test_origin_does_not_accept_cleartext_or_embedded_credentials(value):
    with pytest.raises(ValueError):
        voice_meeting.meeting_origin(value)


def test_participant_page_auto_start_heartbeat_and_leave():
    root = Path(__file__).resolve().parents[1]
    subprocess.run(["node", str(root / "tests/meeting_ui.cjs"),
                    str(root / "src/agentroute/dashboard_assets/meeting.html")], check=True)
