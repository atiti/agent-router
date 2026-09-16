import os

import pytest

from agentroute.config import default_config
from agentroute.providers import (
    END_MARKER,
    START_MARKER,
    backend_readiness,
    import_backend_credential,
    sync_codex_providers,
)


def test_sync_codex_providers_is_idempotent_and_never_writes_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('model = "gpt-5.6-terra"\n', encoding="utf-8")
    config = default_config()
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    config.backends["deepseek"].enabled = True

    sync_codex_providers(config, path, backup=False)
    first = path.read_text(encoding="utf-8")
    sync_codex_providers(config, path, backup=False)
    second = path.read_text(encoding="utf-8")

    assert first == second
    assert first.count(START_MARKER) == 1
    assert first.count(END_MARKER) == 1
    assert "AZURE_OPENAI_API_KEY" in first
    assert "DEEPSEEK_API_KEY" in first
    assert "api-key" in first
    assert "secret" not in first.lower()


def test_imported_credential_is_owner_only_and_counts_as_ready(tmp_path, monkeypatch):
    path = tmp_path / "backend-credentials.env"
    config = default_config()
    config.backends["deepseek"].enabled = True
    monkeypatch.setenv("SOURCE_DEEPSEEK_KEY", "secret with a quote '")
    monkeypatch.setenv("AGENTROUTE_CREDENTIALS_FILE", str(path))

    written, target_env = import_backend_credential(
        config, "deepseek", "SOURCE_DEEPSEEK_KEY"
    )

    assert written == path
    assert target_env == "DEEPSEEK_API_KEY"
    assert path.stat().st_mode & 0o777 == 0o600
    assert "SOURCE_DEEPSEEK_KEY" not in path.read_text(encoding="utf-8")
    assert backend_readiness(config, "deepseek") == (True, [])


def test_imported_credential_refuses_symlink(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.write_text("untouched\n", encoding="utf-8")
    path = tmp_path / "backend-credentials.env"
    os.symlink(target, path)
    config = default_config()
    monkeypatch.setenv("SOURCE_DEEPSEEK_KEY", "secret")

    with pytest.raises(ValueError, match="symlink"):
        import_backend_credential(config, "deepseek", "SOURCE_DEEPSEEK_KEY", path)

    assert target.read_text(encoding="utf-8") == "untouched\n"
