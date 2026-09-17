import os

import pytest
import yaml

from agentroute.config import default_config, load_config
from agentroute.providers import (
    END_MARKER,
    START_MARKER,
    backend_readiness,
    effective_review_model,
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
    assert 'approval_review_model = "gpt-5-mini"' in first
    assert 'approval_review_model = "deepseek-flash"' in first
    assert 'tool_compatibility = "functions_and_apply_patch"' in first
    assert "secret" not in first.lower()


def test_explicit_review_model_overrides_fast_default(tmp_path):
    path = tmp_path / "config.toml"
    config = default_config()
    config.backends["deepseek"].enabled = True
    config.backends["deepseek"].review_model = "validated-reviewer"

    sync_codex_providers(config, path, backup=False)

    rendered = path.read_text(encoding="utf-8")
    assert 'approval_review_model = "validated-reviewer"' in rendered
    assert rendered.count('approval_review_model = "validated-reviewer"') == 1


def test_effective_review_model_uses_managed_gpt_and_fast_api_defaults():
    config = default_config()

    assert effective_review_model(config, "gpt") == "codex-auto-review"
    assert effective_review_model(config, "azure") == "gpt-5-mini"
    assert effective_review_model(config, "deepseek") == "deepseek-flash"


def test_legacy_deepseek_backend_gains_safe_tool_compatibility(tmp_path):
    path = tmp_path / "config.yaml"
    payload = default_config().model_dump(mode="json", exclude_none=True)
    payload["backends"]["deepseek"].pop("tool_compatibility")
    payload["backends"]["deepseek"]["tiers"]["fast"][
        "reasoning_effort"
    ] = "none"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    loaded = load_config(path)

    assert (
        loaded.backends["deepseek"].tool_compatibility
        == "functions_and_apply_patch"
    )
    assert loaded.backends["deepseek"].tiers["fast"].reasoning_effort == "low"


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
