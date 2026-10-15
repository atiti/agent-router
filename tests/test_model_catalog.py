import json
import subprocess
from pathlib import Path

import pytest
import tomllib

from agentroute.config import (
    ExecutionBackendConfig,
    ModelCapabilities,
    ModelTarget,
    default_config,
)
from agentroute.providers import (
    _installed_codex_catalog,
    refresh_launch_catalog,
    sync_codex_providers,
)


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def test_installed_catalog_aliases_exact_commit_and_keeps_root_config(tmp_path, monkeypatch):
    source = tmp_path / "codex"
    source.mkdir()
    git(source, "init")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.test")
    models = source / "codex-rs/models-manager/models.json"
    models.parent.mkdir(parents=True)
    original = {
        "models": [
            {"slug": "gpt-6-luna", "context_window": 42, "capabilities": {"tools": True}},
            {"slug": "gpt-6-astra", "context_window": 99},
        ],
        "extra": "retained",
    }
    models.write_text(json.dumps(original))
    git(source, "add", ".")
    git(source, "commit", "-m", "first catalog")
    installed_commit = git(source, "rev-parse", "HEAD")
    models.write_text(json.dumps({"models": [{"slug": "gpt-6-luna", "context_window": 1}]}))
    git(source, "add", ".")
    git(source, "commit", "-m", "changed catalog")

    home = tmp_path / "agentroute"
    home.mkdir()
    (home / "build-id").write_text(f"{installed_commit}-provider-routing-v58\n")
    monkeypatch.setenv("AGENTROUTE_HOME", str(home))
    monkeypatch.delenv("AGENTROUTE_CODEX_SOURCE", raising=False)
    monkeypatch.delenv("AGENTROUTE_RUNTIME_BUILD_ID", raising=False)
    config = default_config()
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.test/openai/v1"
    config.backends["azure"].tiers["fast"].model = "dev-gpt-6-luna"
    config.pricing.aliases["dev-gpt-6-luna"] = "gpt-6-luna"
    path = tmp_path / "codex-config.toml"
    path.write_text('model = "gpt-6-luna"\n[features]\ngoals = true\n')
    sync_codex_providers(config, path, backup=False, codex_source=source)
    first = path.read_text()
    sync_codex_providers(config, path, backup=False, codex_source=source)
    assert path.read_text() == first
    parsed = tomllib.loads(first)
    catalog_path = Path(parsed["model_catalog_json"])
    assert parsed["features"]["goals"] is True
    assert parsed["model_providers"]["agentroute-azure"]["base_url"].endswith("/openai/v1")
    catalog = json.loads(catalog_path.read_text())
    assert catalog["extra"] == "retained"
    assert catalog["models"][:2] == original["models"]
    assert catalog["models"][2] == {
        **original["models"][0],
        "slug": "dev-gpt-6-luna",
        "display_name": "dev-gpt-6-luna",
    }


def test_installed_catalog_uses_invoking_runtime_build_id(tmp_path, monkeypatch):
    source = tmp_path / "codex"
    source.mkdir()
    git(source, "init")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.test")
    models = source / "codex-rs/models-manager/models.json"
    models.parent.mkdir(parents=True)
    original = {"models": [{"slug": "gpt-6-luna", "context_window": 42}]}
    models.write_text(json.dumps(original))
    git(source, "add", ".")
    git(source, "commit", "-m", "runtime catalog")
    runtime_commit = git(source, "rev-parse", "HEAD")
    models.write_text(json.dumps({"models": [{"slug": "gpt-6-luna", "context_window": 1}]}))
    git(source, "add", ".")
    git(source, "commit", "-m", "new cli catalog")
    cli_commit = git(source, "rev-parse", "HEAD")

    home = tmp_path / "agentroute"
    home.mkdir()
    (home / "build-id").write_text(f"{cli_commit}-provider-routing-v58\n")
    monkeypatch.setenv("AGENTROUTE_HOME", str(home))
    monkeypatch.setenv("AGENTROUTE_RUNTIME_BUILD_ID", f"{runtime_commit}-provider-routing-v58")

    assert _installed_codex_catalog(source) == original


def test_launch_catalog_sidecars_are_scoped_to_invoking_runtime(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    agentroute_home = tmp_path / "agentroute"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("AGENTROUTE_HOME", str(agentroute_home))
    monkeypatch.setattr(
        "agentroute.providers._installed_codex_catalog",
        lambda _source=None: {
            "models": [
                {"slug": "gpt-6.1-sol", "context_window": 272_000, "max_context_window": 872_000}
            ]
        },
    )
    config = default_config()
    config.backends["gpt"].tiers["fast"].model = "gpt-6.1-sol"

    first_commit = "1" * 40
    monkeypatch.setenv("AGENTROUTE_RUNTIME_BUILD_ID", f"{first_commit}-provider-routing-v58")
    first = refresh_launch_catalog(config)
    assert first is not None and first.exists()
    (codex_home / "config.toml").write_text(
        f"model_catalog_json = {json.dumps(str(first))}\n", encoding="utf-8"
    )

    second_commit = "2" * 40
    monkeypatch.setenv("AGENTROUTE_RUNTIME_BUILD_ID", f"{second_commit}-provider-routing-v58")
    second = refresh_launch_catalog(config)

    assert second is not None and second.exists()
    assert second != first
    assert first.exists()


def test_alias_fails_closed_without_installed_catalog(tmp_path, monkeypatch):
    home = tmp_path / "agentroute"
    home.mkdir()
    monkeypatch.setenv("AGENTROUTE_HOME", str(home))
    config = default_config()
    config.backends["azure"].enabled = True
    config.backends["azure"].tiers["fast"].model = "dev-gpt-6-luna"
    config.pricing.aliases["dev-gpt-6-luna"] = "gpt-6-luna"
    with pytest.raises(ValueError, match="installed Codex model catalog"):
        sync_codex_providers(config, tmp_path / "config.toml", backup=False)


def test_sync_other_config_cannot_replace_live_routed_catalog(tmp_path, monkeypatch):
    from agentroute.providers import ensure_claude_bridge_backend

    home = tmp_path / "agentroute"
    monkeypatch.setenv("AGENTROUTE_HOME", str(home))
    monkeypatch.setattr("agentroute.providers._installed_codex_catalog", lambda source: {
        "models": [{"slug": "gpt-6-luna", "context_window": 272_000,
                    "supports_search_tool": True}]
    })
    live = default_config()
    live.backends["azure"].enabled = True
    live.backends["azure"].tiers["fast"].model = "dev-gpt-6-luna"
    live.pricing.aliases["dev-gpt-6-luna"] = "gpt-6-luna"
    live_path = tmp_path / "live" / "config.toml"
    live_path.parent.mkdir()
    sync_codex_providers(live, live_path, backup=False)
    live_catalog = Path(tomllib.loads(live_path.read_text())["model_catalog_json"])
    original = live_catalog.read_bytes()

    fixture = default_config()
    ensure_claude_bridge_backend(fixture, 8090)
    other_path = tmp_path / "fixture" / "config.toml"
    other_path.parent.mkdir()
    sync_codex_providers(fixture, other_path, backup=False)

    assert live_catalog.read_bytes() == original
    other_catalog = Path(tomllib.loads(other_path.read_text())["model_catalog_json"])
    assert other_catalog != live_catalog
    assert next(m for m in json.loads(original)["models"]
                if m["slug"] == "dev-gpt-6-luna")["supports_search_tool"] is True


@pytest.mark.parametrize("age_hours,window,expected", [
    (1, 500_000, 500_000), (25, 500_000, 272_000),
    (1, 2_000_000, 272_000), (1, True, 272_000),
])
def test_backend_alias_uses_configured_context_while_gpt_uses_account_limit(
    tmp_path, monkeypatch, age_hours, window, expected
):
    from datetime import datetime, timedelta, timezone

    from agentroute.providers import refresh_launch_catalog

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr("agentroute.providers._installed_codex_catalog", lambda source: {
        "models": [{"slug": "gpt-6.1-sol", "context_window": 272_000,
                    "max_context_window": 872_000, "auto_compact_token_limit": 258_400,
                    "supports_search_tool": True, "guardian": {"required": True}}]
    })
    (tmp_path / "models_cache.json").write_text(json.dumps({
        "fetched_at": (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat(),
        "models": [{"slug": "gpt-6.1-sol", "context_window": window,
                    "auto_compact_token_limit": 200_000,
                    "supports_search_tool": False, "guardian": None}],
    }))
    config = default_config()
    config.backends["azure"].enabled = True
    config.backends["azure"].tiers["smart"].model = "dev-gpt-6.1-sol"
    config.pricing.aliases["dev-gpt-6.1-sol"] = "gpt-6.1-sol"
    path = refresh_launch_catalog(config)
    models = {m["slug"]: m for m in json.loads(path.read_text())["models"]}
    assert models["gpt-6.1-sol"]["context_window"] == expected
    assert models["gpt-6.1-sol"]["supports_search_tool"] is True
    assert models["gpt-6.1-sol"]["guardian"] == {"required": True}
    assert models["gpt-6.1-sol"]["auto_compact_token_limit"] == (
        200_000 if expected == 500_000 else 258_400
    )
    assert models["dev-gpt-6.1-sol"]["auto_compact_token_limit"] is None
    assert models["dev-gpt-6.1-sol"]["context_window"] == 872_000
    assert models["dev-gpt-6.1-sol"]["max_context_window"] == 872_000
    assert models["dev-gpt-6.1-sol"]["supports_search_tool"] is True
    assert models["dev-gpt-6.1-sol"]["guardian"] == {"required": True}
    first = path.stat().st_mtime_ns
    refresh_launch_catalog(config)
    assert path.stat().st_mtime_ns == first
    path.write_text('{"models": []}')
    refresh_launch_catalog(config)
    assert json.loads(path.read_text())["models"]


def test_launch_preserves_custom_catalog(tmp_path, monkeypatch):
    from agentroute.providers import refresh_launch_catalog

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('model_catalog_json = "/custom/models.json"\n')
    monkeypatch.setattr("agentroute.providers._installed_codex_catalog", lambda source: pytest.fail(
        "custom catalog must not be regenerated"))
    assert refresh_launch_catalog(default_config()) is None


def test_routed_provider_models_use_configured_context_limits_and_tool_modes(
    tmp_path, monkeypatch
):
    from agentroute.providers import refresh_launch_catalog

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr("agentroute.providers._installed_codex_catalog", lambda source: {
        "models": [{"slug": "gpt-6-astra", "context_window": 272_000,
                    "max_context_window": 872_000}],
    })
    config = default_config()
    config.backends["deepseek"].enabled = True
    config.backends["deepseek"].tiers["smart"].model = "deepseek-v4-pro"
    config.backends["qwen"] = ExecutionBackendConfig(
        enabled=True,
        codex_provider="agentroute-qwen",
        display_name="Local Qwen",
        base_url="http://127.0.0.1:8080/v1",
        tool_compatibility="full",
        tiers={tier: ModelTarget(model="mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit")
               for tier in ("fast", "normal", "smart", "max")},
    )
    config.capabilities.models["mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit"] = (
        ModelCapabilities(
            tool_calling="full", reasoning=False, vision=False, context_window=32_768
        )
    )

    path = refresh_launch_catalog(config)
    catalog = json.loads(path.read_text())
    models = {model["slug"]: model for model in catalog["models"]}

    assert models["deepseek-flash"]["context_window"] == 1_048_576
    assert models["deepseek-flash"]["max_context_window"] == 1_048_576
    assert models["deepseek-flash"]["input_modalities"] == ["text", "image"]
    assert models["deepseek-flash"]["tool_mode"] == "direct"
    assert models["deepseek-flash"]["supports_search_tool"] is False
    assert models["deepseek-v4-pro"]["context_window"] == 1_048_576
    qwen = models["mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit"]
    assert qwen["context_window"] == 32_768
    assert qwen["tool_mode"] == "direct"
    assert qwen["supports_search_tool"] is False
