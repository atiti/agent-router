"""Optional real-native probe; model responses and attempted effects are local fixtures."""

import json
import os
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from websockets.sync.client import unix_connect

from agentroute.voice_meeting import coordinator_config


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6-astra"])
def test_native_coordinator_cannot_use_environment_or_inherited_mcp(model):
    binary = os.environ.get("AGENTROUTE_TEST_CODEX_BINARY")
    if not binary:
        pytest.skip("set AGENTROUTE_TEST_CODEX_BINARY for pinned native qualification")
    with tempfile.TemporaryDirectory(prefix="ar-meet-") as temporary:
        root = Path(temporary)
        home, cwd = root / "codex", root / "work"
        home.mkdir()
        cwd.mkdir()
        target = root / "must-not-exist"
        captured = []
        patch = f"*** Begin Patch\n*** Add File: {target}\n+probe\n*** End Patch"
        attempts = [["meeting__status", {}], ["apply_patch", patch],
                    ["list_mcp_resources", {}],
                    ["read_mcp_resource", {"server": "forbidden", "uri": "test://private"}]]
        script = (
            "for (const [name,arg] of " + json.dumps(attempts) + ") {"
            "try {text({name,result:await tools[name](arg)});}"
            "catch(error){text({name,error:String(error)});}}"
        )

        class Responses(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                captured.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                item = ({"type": "custom_tool_call", "id": "probe", "call_id": "probe",
                         "name": "exec", "input": script} if len(captured) == 1 else
                        {"type": "message", "role": "assistant", "id": "answer",
                         "content": [{"type": "output_text", "text": "done"}]})
                events = [{"type": "response.created", "response": {"id": "fixture"}},
                          {"type": "response.output_item.done", "item": item},
                          {"type": "response.completed", "response": {"id": "fixture",
                           "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}}]
                payload = "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        http = ThreadingHTTPServer(("127.0.0.1", 0), Responses)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        (home / "config.toml").write_text(f'''model_provider = "fixture"
model = "{model}"
approval_policy = "never"
[model_providers.fixture]
name = "Local fixture"
base_url = "http://127.0.0.1:{http.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
[mcp_servers.forbidden]
command = "/not/allowed/to/start"
enabled = true
''')
        env = {k: v for k, v in os.environ.items() if not any(
            marker in k.upper() for marker in
            ("TOKEN", "SECRET", "API_KEY", "PASSWORD", "AGENTROUTE", "CODEX"))}
        env.update(CODEX_HOME=str(home), AGENTROUTE_HOME=str(root / "route"))
        socket = root / "server.sock"
        with (root / "native.log").open("w+") as log:
            process = subprocess.Popen([binary, "app-server", "--listen", f"unix://{socket}"],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env)
            try:
                deadline = time.monotonic() + 20
                while (not socket.exists() and process.poll() is None
                       and time.monotonic() < deadline):
                    time.sleep(0.05)
                assert socket.exists(), "native fixture did not start"
                with unix_connect(str(socket), open_timeout=3, max_size=64 * 1024 * 1024) as client:
                    sequence = 0

                    def receive():
                        event = json.loads(client.recv(timeout=20))
                        if event.get("method") == "item/tool/call":
                            assert event["params"]["namespace"] == "meeting"
                            assert event["params"]["tool"] == "status"
                            client.send(json.dumps({"id": event["id"], "result": {
                                "success": True, "contentItems": [{"type": "inputText",
                                                                   "text": "ALLOWED_TOOL_OK"}],
                            }}))
                        return event

                    def request(method, params):
                        nonlocal sequence
                        sequence += 1
                        client.send(json.dumps({"id": sequence, "method": method,
                                                "params": params}))
                        while True:
                            event = receive()
                            if (event.get("id") == sequence
                                    and ("result" in event or "error" in event)):
                                assert "error" not in event, event
                                return event["result"]

                    request("initialize", {
                        "clientInfo": {"name": "meeting_fixture", "version": "1"},
                        "capabilities": {"experimentalApi": True},
                    })
                    client.send(json.dumps({"method": "initialized"}))
                    effective = request("config/read", {"cwd": str(cwd), "includeLayers": False})
                    thread = request("thread/start", {
                        "cwd": str(cwd), "model": model, "modelProvider": "fixture",
                        "ephemeral": True, "sandbox": "read-only", "environments": [],
                        "runtimeWorkspaceRoots": [],
                        "config": coordinator_config(effective["config"]),
                        "dynamicTools": [{"namespace": "meeting", "name": "status",
                                          "description": "Fixture status", "inputSchema": {
                                              "type": "object", "properties": {}}}],
                    })["thread"]
                    request("turn/start", {"threadId": thread["id"], "input": [{
                        "type": "text", "text": "Exercise fixture tools only."}]})
                    while len(captured) < 2:
                        receive()
                evidence = json.dumps(captured[-1])
                assert "ALLOWED_TOOL_OK" in evidence
                assert "No usable execution environment" in evidence
                assert "unknown MCP server" in evidence
                assert not target.exists()
                assert "<skills_instructions>" not in evidence
                assert "<memory" not in evidence
            finally:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                http.shutdown()
