"""Exercise folder consent through the real shared-owner launcher and terminal."""

import json
import os
import re
import select
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agentroute.config import default_config, save_config
from agentroute.shared_server import RpcClient, ensure_server, stop_server


@pytest.mark.skipif(os.name != "posix", reason="shared Unix owner requires POSIX")
@pytest.mark.parametrize("mode", ["start", "resume", "fork"])
def test_real_local_shared_owner_resolves_untrusted_ancestor_before_creating_task(
    monkeypatch, mode
):
    import fcntl
    import pty
    import termios

    binary_value = os.environ.get("AGENTROUTE_TEST_CODEX_BINARY")
    if not binary_value:
        pytest.skip("set AGENTROUTE_TEST_CODEX_BINARY to run against the patched runtime")
    binary = Path(binary_value)
    with tempfile.TemporaryDirectory(prefix="ar-trust-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        home = root / "codex"
        project = root / "build" / "project"
        nested = project / "nested"
        home.mkdir()
        nested.mkdir(parents=True)
        launch_folder = root / "launch"
        launch_folder.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        monkeypatch.setenv("AGENTROUTE_HOME", str(root / "agentroute"))
        monkeypatch.setenv("CODEX_HOME", str(home))
        monkeypatch.setenv("AGENTROUTE_CONFIG", str(root / "config.yaml"))
        monkeypatch.setenv("AGENTROUTE_CREDENTIALS_FILE", str(root / "missing-credentials"))
        monkeypatch.delenv("AGENTROUTE_RUNTIME_BUILD_ID", raising=False)
        monkeypatch.delenv("CODEX_ROLLOUT_TRACE_ROOT", raising=False)
        config = default_config()
        config.shared_server.enabled = True
        config.backends["gpt"].codex_provider = "test"
        config.backends["gpt"].tiers["normal"].model = "gpt-5"
        saved = f"""model_provider = "test"
model = "gpt-5"
[tui]
screen_reader_detection_done = true
[model_providers.test]
name = "Mock"
base_url = "http://127.0.0.1:1/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
[projects.{json.dumps(str(project.parent))}]
trust_level = "untrusted"
[projects.{json.dumps(str(launch_folder))}]
trust_level = "trusted"
"""
        (home / "config.toml").write_text(saved)
        save_config(config)
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
        environment = os.environ.copy()
        environment.update(TERM="xterm-256color", RUST_LOG="trace")
        process = None
        output = b""
        try:
            endpoint = ensure_server(binary, config)
            args = []
            with RpcClient(endpoint) as client:
                if mode != "start":
                    thread = client.request("thread/start", {"cwd": str(nested)})["thread"]
                    args = [mode, thread["id"]]
                loaded_before = client.request("thread/loaded/list", {})["data"]
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "agentroute",
                    "launch-codex",
                    "--binary",
                    str(binary),
                    "--",
                    *args,
                    "--no-alt-screen",
                    "-c",
                    f'log_dir="{root / "logs"}"',
                ],
                cwd=nested if mode == "start" else launch_folder,
                env=environment,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
            os.close(slave)
            slave = None
            deadline = time.monotonic() + 45
            plain = ""
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    output += chunk
                    if b"\x1b[6n" in chunk:
                        os.write(master, b"\x1b[1;1R")
                    plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output.decode(errors="replace"))
                    if "Trust and continue" in plain or process.poll() is not None:
                        break
            assert "Trust and continue" in plain, plain
            assert "Trusting will apply to the repository root" in plain, plain
            root_caption = "Trusting will apply to the repository root:"
            root_display = plain.split(root_caption, 1)[1]
            assert re.search(re.escape(str(project)) + r"(?=\s|$)", root_display), plain
            assert "explicitly untrusted project; pass" not in plain, plain
            assert (home / "config.toml").read_text() == saved
            with RpcClient(endpoint) as client:
                assert client.request("thread/loaded/list", {})["data"] == loaded_before
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            os.close(master)
            if slave is not None:
                os.close(slave)
            stop_server(force=True)
