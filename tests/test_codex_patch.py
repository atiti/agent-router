from agentroute.codex_patch import patch_path


def test_native_patch_is_packaged():
    content = patch_path().read_text()

    assert "reasoningEffort" in content
    assert "settings_updated" in content
    assert "UserPromptSubmit" in content
