import json
import os
import select
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agentroute.config import default_config, load_config, save_config
from agentroute.launcher import (
    desktop_server_launch,
    interactive_launch,
    launch_codex,
    shared_client_args,
    validate_desktop_startup,
)
from agentroute.shared_server import (
    RpcClient,
    SharedServerError,
    ensure_server,
    server_argv,
    server_environment,
    server_status,
    state_dir,
    stop_server,
)


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["hello"],
        ["resume", "id"],
        ["fork", "--last"],
        ["--model", "exec", "hello"],
        ["--", "exec"],
    ],
)
def test_interactive_launches(args):
    assert interactive_launch(args)


@pytest.mark.parametrize(
    "args",
    [
        ["exec", "hello"],
        ["e", "hello"],
        ["login"],
        ["app-server", "--remote-control"],
        ["remote-control", "pair"],
        ["--config", 'model="exec"', "exec", "hello"],
        ["--version"],
        ["resume", "--help"],
    ],
)
def test_utilities_do_not_attach(args):
    assert not interactive_launch(args)


def test_launcher_attaches_and_preserves_cwd_model_effort(tmp_path, monkeypatch):
    config = default_config()
    config.shared_server.enabled = True
    monkeypatch.setattr("agentroute.launcher.load_config", lambda: config)
    monkeypatch.setattr("agentroute.shared_server.ensure_server", lambda *a: tmp_path / "s.sock")
    monkeypatch.setattr("agentroute.shared_server.socket_path", lambda: tmp_path / "s.sock")
    monkeypatch.chdir(tmp_path)
    captured = {}
    monkeypatch.setattr(
        "agentroute.launcher.os.execve", lambda binary, argv, env: captured.update(argv=argv)
    )
    launch_codex(Path("/tmp/codex"), ["--model", "custom", "-c", 'model_reasoning_effort="high"'])
    argv = captured["argv"]
    assert argv[1:5] == ["--remote", f"unix://{tmp_path}/s.sock", "--cd", str(tmp_path)]
    assert argv.count("--model") == 1
    assert 'model_reasoning_effort="high"' in argv


def test_explicit_remote_does_not_start_local_owner(tmp_path, monkeypatch):
    config = default_config()
    config.shared_server.enabled = True
    monkeypatch.setattr("agentroute.launcher.load_config", lambda: config)
    monkeypatch.setattr(
        "agentroute.shared_server.ensure_server", lambda *a: pytest.fail("must not start")
    )
    monkeypatch.setattr("agentroute.launcher.os.execve", lambda *a: None)
    launch_codex(Path("/tmp/codex"), ["--remote", "unix:///custom.sock"])


def test_routed_desktop_joins_canonical_owner_with_stdio_proxy(tmp_path, monkeypatch):
    config = default_config()
    config.shared_server.enabled = True
    config.shared_server.analytics_default_enabled = True
    binary = tmp_path / "desktop" / "codex-bin"
    binary.parent.mkdir()
    (binary.parent / "agentroute-build-id").write_text("build")
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path))
    monkeypatch.setattr("agentroute.launcher.load_config", lambda: config)
    monkeypatch.setattr("agentroute.shared_server.socket_path", lambda: tmp_path / "s.sock")
    captured = {}

    def ensure(owner_binary, *args):
        captured["owner"] = owner_binary
        return tmp_path / "s.sock"

    monkeypatch.setattr("agentroute.shared_server.ensure_server", ensure)
    monkeypatch.setattr(
        "agentroute.launcher.os.execve", lambda binary, argv, env: captured.update(argv=argv)
    )
    launch_codex(binary, ["app-server", "--listen", "stdio://", "--analytics-default-enabled"])
    assert captured["owner"] == tmp_path / "bin" / "codex-bin"
    assert captured["argv"] == [
        sys.executable,
        "-m",
        "agentroute.shared_server",
        "proxy",
        str(tmp_path / "s.sock"),
    ]
    for args in [
        ["app-server", "proxy"],
        ["app-server", "daemon", "start"],
        ["app-server", "--listen", "ws://127.0.0.1:1234"],
        ["app-server", "--help"],
    ]:
        assert not desktop_server_launch(binary, args)


def test_desktop_startup_settings_are_preserved_or_reported(tmp_path):
    config = default_config()
    plugin = "plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled"
    requested = [
        "-c",
        "features.code_mode_host=true",
        "app-server",
        "--analytics-default-enabled",
        "-c",
        f"{plugin}=true",
    ]
    with pytest.raises(SharedServerError, match=plugin):
        validate_desktop_startup(requested, config)
    config.shared_server.startup_config = [f"{plugin}=true"]
    config.shared_server.analytics_default_enabled = True
    validate_desktop_startup(requested, config)
    argv = server_argv(tmp_path / "codex-bin", config)
    assert f"{plugin}=true" in argv
    assert "--analytics-default-enabled" in argv
    with pytest.raises(SharedServerError, match="features.code_mode_host"):
        validate_desktop_startup(["--disable", "code_mode_host", "app-server"], config)


def test_configure_updates_overrides_without_restarting(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agentroute.server_cli import server_app

    monkeypatch.setenv("AGENTROUTE_CONFIG", str(tmp_path / "config.yaml"))
    config = default_config()
    config.shared_server.startup_config = ["existing=true", "changed=false"]
    save_config(config)
    result = CliRunner().invoke(
        server_app,
        [
            "configure",
            "--codex-config",
            "changed=true",
            "--codex-config",
            "new=true",
            "--analytics-default-enabled",
        ],
    )
    assert result.exit_code == 0, result.output
    configured = load_config().shared_server
    assert configured.startup_config == ["changed=true", "existing=true", "new=true"]
    assert configured.analytics_default_enabled is True
    result = CliRunner().invoke(server_app, ["configure", "--no-analytics-default-enabled"])
    assert result.exit_code == 0, result.output
    assert load_config().shared_server.analytics_default_enabled is False
    result = CliRunner().invoke(server_app, ["configure", "--codex-config", "invalid"])
    assert result.exit_code != 0
    assert load_config().shared_server.startup_config == configured.startup_config


def test_client_preserves_explicit_cwd_and_rejects_ignored_provider(tmp_path):
    assert shared_client_args(["--cd", "/project"], tmp_path / "s") == [
        "--remote",
        f"unix://{tmp_path}/s",
    ]
    with pytest.raises(SharedServerError, match="server's provider"):
        shared_client_args(["-c", "model_provider=azure"], tmp_path / "s")
    assert shared_client_args(["resume", "thread-id"], tmp_path / "s") == [
        "--remote",
        f"unix://{tmp_path}/s",
    ]


def test_environment_loads_literal_credentials_without_shell_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTROUTE_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    (tmp_path / "credentials").write_text("export AR_TEST_KEY='$(touch /tmp/should-not-exist)'\n")
    env = server_environment({})
    assert env["AR_TEST_KEY"] == "$(touch /tmp/should-not-exist)"
    assert server_environment({"AR_TEST_KEY": "older"})["AR_TEST_KEY"] == env["AR_TEST_KEY"]


def test_distinct_codex_homes_get_distinct_socket_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "one"))
    first = state_dir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "two"))
    assert state_dir() != first


def _wait_completed(client, turn_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        events, client.events = client.events, []
        for event in events:
            if event.get("method") == "turn/completed" and event["params"]["turn"]["id"] == turn_id:
                assert event["params"]["turn"]["status"] == "completed", event
                return
        client.events.append(json.loads(client.connection.recv(timeout=15)))
    pytest.fail("no completion notification")


def _verify_stdio_proxy(endpoint, thread_id):
    process = subprocess.Popen(
        [sys.executable, "-m", "agentroute.shared_server", "proxy", str(endpoint)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )

    def request(request_id, method, params):
        process.stdin.write(
            (json.dumps({"id": request_id, "method": method, "params": params}) + "\n").encode()
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert select.select([process.stdout], [], [], 10)[0], "proxy did not answer"
            message = json.loads(process.stdout.readline())
            if message.get("id") == request_id and "result" in message:
                return message["result"]
            assert "error" not in message, message
        pytest.fail("no proxy RPC response")

    try:
        request(
            1,
            "initialize",
            {
                "clientInfo": {"name": "Codex Desktop", "version": "test"},
                "capabilities": {"experimentalApi": True},
            },
        )
        process.stdin.write(b'{"method":"initialized"}\n')
        resumed = request(2, "thread/resume", {"threadId": thread_id})
        assert resumed["thread"]["id"] == thread_id
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)


def test_real_two_clients_share_owner_history_and_turns(tmp_path, monkeypatch):
    binary_value = os.environ.get("AGENTROUTE_TEST_CODEX_BINARY")
    if not binary_value:
        pytest.skip("set AGENTROUTE_TEST_CODEX_BINARY to run against the patched runtime")
    binary = Path(binary_value)
    # pytest's default path can exceed macOS's Unix socket limit.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="ar-rc-", dir="/tmp") as temporary:
        root = Path(temporary)
        monkeypatch.setenv("AGENTROUTE_HOME", str(root))
        monkeypatch.setenv("CODEX_HOME", str(root / "codex"))
        monkeypatch.setenv("AGENTROUTE_CONFIG", str(root / "config.yaml"))
        monkeypatch.setenv("AGENTROUTE_CREDENTIALS_FILE", str(root / "missing-credentials"))
        monkeypatch.delenv("AGENTROUTE_RUNTIME_BUILD_ID", raising=False)
        for variable in (
            "CODEX_ROLLOUT_TRACE_ROOT",
            "CODEX_ROLLOUT_TRACE_MAX_BYTES",
            "CODEX_ROLLOUT_TRACE_ENABLED_FILE",
            "AGENTROUTE_PROFILE_CAPTURE",
        ):
            monkeypatch.delenv(variable, raising=False)
        home = root / "codex"
        home.mkdir()
        requests = []

        class Responses(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                n = len(requests)
                events = [
                    {"type": "response.created", "response": {"id": f"resp-{n}"}},
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "message",
                            "role": "assistant",
                            "id": f"msg-{n}",
                            "content": [{"type": "output_text", "text": f"reply-{n}"}],
                        },
                    },
                    {
                        "type": "response.completed",
                        "response": {
                            "id": f"resp-{n}",
                            "usage": {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11},
                        },
                    },
                ]
                body = "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        http = ThreadingHTTPServer(("127.0.0.1", 0), Responses)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        config = default_config()
        config.backends["gpt"].codex_provider = "test"
        config.backends["gpt"].tiers["normal"].model = "gpt-5"
        (home / "config.toml").write_text(f"""model_provider = "test"
model = "gpt-5"
[model_providers.test]
name = "Mock"
base_url = "http://127.0.0.1:{http.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
""")
        save_config(config)
        from agentroute.profiling import set_capture
        from agentroute.storage import file_sizes

        set_capture(True)
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                paths = list(executor.map(lambda _: ensure_server(binary, config), range(2)))
            assert paths[0] == paths[1]
            owner_pid = server_status()["pid"]
            assert ensure_server(binary, config) == paths[0]
            assert server_status()["pid"] == owner_pid
            assert state_dir().stat().st_mode & 0o777 == 0o700
            assert paths[0].stat().st_mode & 0o777 == 0o600
            with RpcClient(paths[0], "codex-tui", timeout=15) as terminal:
                started = terminal.request(
                    "thread/start",
                    {
                        "cwd": str(root),
                        "model": "gpt-5",
                        "historyMode": "paginated",
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                    },
                )
                thread_id = started["thread"]["id"]
                assert started["cwd"] == str(root)
                assert started["modelProvider"] == "test"
                first = terminal.request(
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": "initial terminal prompt"}],
                    },
                )
                _wait_completed(terminal, first["turn"]["id"])
                from typer.testing import CliRunner

                from agentroute.work_cli import owners, work_app

                work = "https://example.com/issues/shared-work"
                linked = CliRunner().invoke(work_app, ["link", work, "--thread", thread_id])
                assert linked.exit_code == 0, linked.output
                assert owners(terminal, work) == [{"threadId": thread_id, "archived": False}]
                sibling = terminal.request("thread/fork", {"threadId": thread_id})["thread"]["id"]
                linked = CliRunner().invoke(work_app, ["link", work, "--thread", sibling])
                assert linked.exit_code == 0, linked.output
                assert {owner["threadId"] for owner in owners(terminal, work)} == {
                    thread_id,
                    sibling,
                }
                _verify_stdio_proxy(paths[0], thread_id)
                trace_root = root / "profiling" / "traces"
                # Capture can be stopped during an existing native owner session.
                set_capture(False)
                captured_bytes = file_sizes(trace_root)[0]
                assert captured_bytes > 8
                with RpcClient(paths[0], "codex_chatgpt_ios_remote", timeout=15) as mobile:
                    resumed = mobile.request("thread/resume", {"threadId": thread_id})
                    assert resumed["thread"]["id"] == thread_id
                    with pytest.raises(SharedServerError, match="loaded sessions"):
                        stop_server()
                    for client, prompt in [
                        (terminal, "terminal prompt"),
                        (mobile, "mobile prompt"),
                    ]:
                        turn = client.request(
                            "turn/start",
                            {
                                "threadId": thread_id,
                                "input": [{"type": "text", "text": prompt}],
                            },
                        )
                        for observer in (terminal, mobile):
                            _wait_completed(observer, turn["turn"]["id"])
                    # Rejoin the same in-memory owner and see both completed turns.
                    history = terminal.request("thread/turns/list", {"threadId": thread_id})
                    turns = history["data"]
                    assert len(turns) == 3
                    assert all(t["status"] == "completed" for t in turns)
                    assert "terminal prompt" in json.dumps(turns)
                    assert "mobile prompt" in json.dumps(turns)
                    assert len(requests) == 3
                    assert file_sizes(trace_root)[0] == captured_bytes
                    assert "terminal prompt" in json.dumps(requests[1])
                    assert "reply-1" in json.dumps(requests[1])
                    changed = config.model_copy(deep=True)
                    changed.backends["gpt"].tiers["normal"].model = "changed"
                    with pytest.raises(SharedServerError, match="runtime/settings differ"):
                        ensure_server(binary, changed)
                    assert server_status()["pid"] == owner_pid
        finally:
            stop_server(force=True)
            http.shutdown()
