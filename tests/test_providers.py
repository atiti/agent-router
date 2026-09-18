import os

import pytest
import yaml

from agentroute.config import default_config, load_config, model_capabilities
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


def test_capability_registry_declares_agent_tool_safety_and_pricing():
    config = default_config()

    gpt = model_capabilities(config, "gpt", "gpt-5.6-terra")
    deepseek = model_capabilities(config, "deepseek", "deepseek-flash")

    assert gpt.tool_calling == "full"
    assert gpt.pricing_model == "gpt-5.6-terra"
    assert deepseek.tool_calling == "apply_patch_only"
    assert deepseek.context_window == 128_000
    assert deepseek.pricing_model == "deepseek-flash"


def test_azure_deployment_prefix_resolves_capabilities_and_pricing_alias(tmp_path):
    path = tmp_path / "config.yaml"
    payload = default_config().model_dump(mode="json", exclude_none=True)
    payload["backends"]["azure"]["enabled"] = True
    payload["backends"]["azure"]["tiers"]["fast"]["model"] = "dev-gpt-5.6-luna"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    config = load_config(path)
    capabilities = model_capabilities(config, "azure", "dev-gpt-5.6-luna")

    assert capabilities.tool_calling == "full"
    assert capabilities.pricing_model == "gpt-5.6-luna"
    assert config.pricing.aliases["dev-gpt-5.6-luna"] == "gpt-5.6-luna"


def test_previous_bundled_deepseek_mapping_migrates_to_v41_flash(tmp_path):
    path = tmp_path / "config.yaml"
    payload = default_config().model_dump(mode="json", exclude_none=True)
    payload["backends"]["deepseek"]["tiers"] = {
        "fast": {"model": "deepseek-flash", "reasoning_effort": "low"},
        "normal": {"model": "deepseek-flash", "reasoning_effort": "low"},
        "smart": {"model": "deepseek-v4-pro", "reasoning_effort": "high"},
        "max": {"model": "deepseek-v4-pro", "reasoning_effort": "max"},
    }
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    loaded = load_config(path)

    assert {
        tier: target.model
        for tier, target in loaded.backends["deepseek"].tiers.items()
    } == {
        "fast": "deepseek-flash",
        "normal": "deepseek-flash",
        "smart": "deepseek-flash",
        "max": "deepseek-flash",
    }
    assert loaded.backends["deepseek"].tiers["normal"].reasoning_effort == "medium"
    assert loaded.pricing.models["deepseek-flash"].input_per_million == 0.30
    assert loaded.pricing.models["deepseek-flash"].output_per_million == 1.20
    assert loaded.pricing.models["deepseek-v4-pro"].input_per_million == 1.32
    assert loaded.pricing.models["deepseek-v4-pro"].output_per_million == 3.96


def test_custom_deepseek_mapping_is_not_migrated(tmp_path):
    path = tmp_path / "config.yaml"
    payload = default_config().model_dump(mode="json", exclude_none=True)
    payload["backends"]["deepseek"]["tiers"]["smart"]["model"] = "custom-smart"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    loaded = load_config(path)

    assert loaded.backends["deepseek"].tiers["smart"].model == "custom-smart"


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
