import json

import pytest

from agentroute.install import merge_codex_hook, trust_agentroute_hooks


def test_hook_install_preserves_existing_hooks_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path / ".agentroute"))
    path = tmp_path / "hooks.json"
    path.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing"}]}]}})
    )

    _, backup = merge_codex_hook(path)
    _, second_backup = merge_codex_hook(path)
    payload = json.loads(path.read_text())

    assert backup is not None and backup.exists()
    assert second_backup is not None and second_backup.exists()
    assert payload["hooks"]["Stop"][0]["hooks"][0]["command"] == "existing"
    stop_commands = [
        item["command"]
        for group in payload["hooks"]["Stop"]
        for item in group["hooks"]
    ]
    assert sum(command.endswith("agentroute hook codex stop") for command in stop_commands) == 1
    groups = payload["hooks"]["UserPromptSubmit"]
    commands = [item["command"] for group in groups for item in group["hooks"]]
    assert len(commands) == 1
    assert commands[0].endswith("/.agentroute/bin/agentroute hook codex user-prompt-submit")


def test_trust_agentroute_hooks_only_writes_exact_installed_commands(tmp_path, monkeypatch):
    agentroute_home = tmp_path / ".agentroute"
    monkeypatch.setenv("AGENTROUTE_HOME", str(agentroute_home))
    hooks_path = tmp_path / ".codex" / "hooks.json"
    hooks_path.parent.mkdir()
    hooks_path.write_text("{}")
    prompt_command = f"{agentroute_home}/bin/agentroute hook codex user-prompt-submit"
    stop_command = f"{agentroute_home}/bin/agentroute hook codex stop"
    requests = []

    class FakeClient:
        def __init__(self, binary):
            assert binary == tmp_path / "codex-bin"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, params):
            requests.append((method, params))
            if method == "hooks/list":
                return {
                    "data": [
                        {
                            "hooks": [
                                {
                                    "key": f"{hooks_path}:user_prompt_submit:0:0",
                                    "eventName": "userPromptSubmit",
                                    "handlerType": "command",
                                    "command": prompt_command,
                                    "sourcePath": str(hooks_path),
                                    "currentHash": "sha256:prompt",
                                },
                                {
                                    "key": f"{hooks_path}:stop:0:0",
                                    "eventName": "stop",
                                    "handlerType": "command",
                                    "command": stop_command,
                                    "sourcePath": str(hooks_path),
                                    "currentHash": "sha256:stop",
                                },
                                {
                                    "key": f"{hooks_path}:stop:1:0",
                                    "eventName": "preToolUse",
                                    "handlerType": "command",
                                    "command": stop_command,
                                    "sourcePath": str(hooks_path),
                                    "currentHash": "sha256:other",
                                },
                            ]
                        }
                    ]
                }
            return {}

    monkeypatch.setattr("agentroute.install._CodexAppServer", FakeClient)

    count = trust_agentroute_hooks(
        tmp_path / "codex-bin", hooks_path=hooks_path, cwd=tmp_path
    )

    assert count == 2
    assert requests[0] == ("hooks/list", {"cwds": [str(tmp_path)]})
    assert requests[1][0] == "config/batchWrite"
    assert requests[1][1]["edits"][0]["value"] == {
        f"{hooks_path}:user_prompt_submit:0:0": {"trusted_hash": "sha256:prompt"},
        f"{hooks_path}:stop:0:0": {"trusted_hash": "sha256:stop"},
    }


def test_trust_agentroute_hooks_fails_if_an_expected_hook_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path / ".agentroute"))
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text("{}")

    class FakeClient:
        def __init__(self, _binary):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, _params):
            if method == "hooks/list":
                return {"data": [{"hooks": []}]}
            raise AssertionError("trust state must not be written")

    monkeypatch.setattr("agentroute.install._CodexAppServer", FakeClient)

    try:
        trust_agentroute_hooks(tmp_path / "codex-bin", hooks_path=hooks_path, cwd=tmp_path)
    except RuntimeError as error:
        assert "did not discover" in str(error)
    else:
        raise AssertionError("expected missing hooks to fail closed")


def test_app_server_reports_exit_when_stdin_is_broken(tmp_path):
    from agentroute.install import _CodexAppServer

    class ClosedPipe:
        def write(self, _value):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            return None

    class FakeProcess:
        stdin = ClosedPipe()
        stderr = None

        def poll(self):
            return -9

    client = object.__new__(_CodexAppServer)
    client.process = FakeProcess()

    with pytest.raises(RuntimeError, match="exited before receiving hooks/list.*-9"):
        client._send({"method": "hooks/list"})
