import json
import subprocess
from pathlib import Path

import pytest
import tomllib

from agentroute.config import default_config
from agentroute.providers import sync_codex_providers


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
    (home / "build-id").write_text(f"{installed_commit}-provider-routing-v57\n")
    monkeypatch.setenv("AGENTROUTE_HOME", str(home))
    monkeypatch.delenv("AGENTROUTE_CODEX_SOURCE", raising=False)
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
