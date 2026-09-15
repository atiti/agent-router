import json

from agentroute.install import merge_codex_hook


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
    groups = payload["hooks"]["UserPromptSubmit"]
    commands = [item["command"] for group in groups for item in group["hooks"]]
    assert len(commands) == 1
    assert commands[0].endswith("/.agentroute/bin/agentroute hook codex user-prompt-submit")
