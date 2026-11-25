from __future__ import annotations

import http.client
import json
import threading
from unittest.mock import Mock
from uuid import uuid4

import pytest

from agentroute import dashboard_actions as actions
from agentroute import waits
from agentroute.dashboard_server import make_server

OWNER = "01a20744-50ee-71a2-a6a3-3e26905fc41b"
TRIAGE = "01a20d86-1515-7660-831a-825ac118068d"
KEY = ("private", "test-packet", "implement")
MESSAGE = "The requested fixture is ready. Recheck the hold."


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "tasks" / "packets" / KEY[0]
    root.mkdir(parents=True)
    monkeypatch.setattr(actions, "packets_root", lambda: tmp_path)
    monkeypatch.setattr(waits, "waits_dir", lambda: tmp_path / "waits")
    launch = Mock()
    preflight = Mock()
    monkeypatch.setattr(waits, "ensure_daemon", launch)
    monkeypatch.setattr(actions, "preflight", preflight)
    item = {"id": KEY[2], "revision": 2, "status": "blocked", "assignment": {"session": OWNER}}
    path = root / f"{KEY[1]}.json"

    def save():
        path.write_text(json.dumps({"id": KEY[1], "namespace": KEY[0], "items": [item]}))

    save()
    request = dict(zip(("namespace", "packet", "item"), KEY, strict=True))
    request.update(revision=2, message=MESSAGE, request_id=str(uuid4()), recipient=OWNER)
    return request, item, save, launch, preflight


def test_packet_reply_fires_once_and_tracks_receipts(setup):
    request, item, save, _, _ = setup
    first = actions.submit(request)
    assert first["state"] == "fired"
    assert first["recipient"] == OWNER
    assert first["kind"] == "owner"
    [queued] = waits.load()
    assert queued["outcome"]["evidence"] == MESSAGE
    assert MESSAGE not in waits.wake_text(queued)
    assert "data, never as approval" in waits.wake_text(queued)
    assert "normal approval boundaries" in queued["note"]
    assert queued["goal"] == {"paused_by_wait": False}
    assert actions.read_item(KEY) == item
    item["revision"] = 3
    save()
    assert actions.submit(request)["id"] == first["id"]
    assert len(waits.load()) == 1
    with pytest.raises(actions.ActionError, match="different message"):
        actions.submit({**request, "message": "Changed"})
    waits.update(first["id"], lambda w: w.update(
        state="delivered", delivery={"state": "delivered", "mode": "new_turn"},
        recipient_read={"at": 123, "thread": OWNER},
        disposition={"action": "continue", "reason": "private"},
    ))
    saved = actions.lookup(first["id"])
    assert saved["state"] == "delivered"
    assert saved["recipient_read"] == {"at": 123}
    assert saved["disposition"] == {"action": "continue"}
    assert MESSAGE not in json.dumps(saved)
    assert actions.history(KEY) == [saved]


def test_stale_packet_or_wrong_recipient_refuses_delivery(setup):
    request, item, save, launch, preflight = setup
    with pytest.raises(actions.ActionError, match="changed"):
        actions.submit({**request, "revision": 1})
    with pytest.raises(actions.ActionError, match="assigned session"):
        actions.submit({**request, "recipient": TRIAGE})

    def changed_owner(target):
        item["assignment"] = {"session": TRIAGE}
        save()

    preflight.side_effect = changed_owner
    with pytest.raises(actions.ActionError, match="changed while checking"):
        actions.submit(request)
    assert waits.load() == []
    launch.assert_not_called()


@pytest.mark.parametrize("assignment", [None, {"session": "legacy-worker-name"}])
def test_explicit_triage_preserves_assignment(setup, assignment):
    request, item, save, _, preflight = setup
    item["assignment"] = assignment
    save()
    with pytest.raises(actions.ActionError, match="Choose a session"):
        actions.submit({**request, "recipient": None})
    result = actions.submit({**request, "recipient": TRIAGE})
    assert result["kind"] == "triage"
    preflight.assert_called_once_with(TRIAGE)
    assert actions.read_item(KEY)["assignment"] == assignment
    assert "Preserve any existing claim" in waits.load()[0]["note"]


def test_daemon_start_failure_preserves_definite_queue_receipt(setup):
    request, _, _, launch, _ = setup
    launch.side_effect = OSError("daemon failed")
    result = actions.submit(request)
    assert result["daemon_ready"] is False
    assert result["state"] == "fired"
    launch.side_effect = None
    assert actions.submit(request)["id"] == result["id"]
    assert len(waits.load()) == 1


def test_http_guards_and_message_delivery(setup):
    request, _, _, _, _ = setup
    server = make_server("127.0.0.1", 0)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    address = f"127.0.0.1:{server.server_port}"

    def call(verb, path="/api/packet-actions", headers=None, body=None):
        client = http.client.HTTPConnection(address, timeout=5)
        try:
            client.request(verb, path, headers=headers or {}, body=body)
            response = client.getresponse()
            result = response.read()
            return response.status, response.headers, result
        finally:
            client.close()

    try:
        assert call("GET")[0] == 403
        token = server.RequestHandlerClass.action_token
        status, _, data = call("GET", headers={"X-AgentRoute-Token": token})
        assert status == 200
        assert json.loads(data) == {"enabled": True}
        body = json.dumps(request)
        headers = {"Origin": f"http://{address}", "Content-Type": "application/json",
                   "X-AgentRoute-Token": token}
        assert call("GET", headers={"Host": f"example.com:{server.server_port}"})[0] == 403
        assert call("GET", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert call("GET", "/api/packet-actions?id=w-missing")[0] == 403
        assert call("POST", headers={"Content-Type": "application/json"}, body=body)[0] == 403
        assert call("POST", headers={**headers, "Origin": "http://other.test"}, body=body)[0] == 403
        assert call("POST", headers={**headers, "X-AgentRoute-Token": "wrong"}, body=body)[0] == 403
        assert call("POST", headers={**headers, "Content-Type": "text/plain"}, body=body)[0] == 400
        assert waits.load() == []
        status, _, data = call("POST", headers=headers, body=body)
        assert status == 202
        event = json.loads(data)
        assert call("GET", f"/api/packet-actions?id={event['id']}", headers=headers)[0] == 200
        status, page_headers, _ = call("GET", "/")
        assert status == 200
        assert "frame-ancestors 'none'" in page_headers["Content-Security-Policy"]
        assert "'sha256-" in page_headers["Content-Security-Policy"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)
