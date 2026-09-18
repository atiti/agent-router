from typer.testing import CliRunner

from agentroute.cli import app
from agentroute.config import default_config, load_config, save_config

runner = CliRunner()


def test_backend_default_routes_every_tier_to_ready_backend(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(path))
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    config = default_config()
    config.backends["azure"].enabled = True
    config.backends["azure"].base_url = "https://example.openai.azure.com/openai/v1"
    save_config(config, path)

    result = runner.invoke(app, ["backend-default", "azure"])

    assert result.exit_code == 0
    assert "All tiers now default to azure" in result.output
    assert set(load_config(path).routing.backend_by_tier.values()) == {"azure"}


def test_backend_default_rejects_unready_backend(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(path))
    monkeypatch.setenv("AGENTROUTE_CREDENTIALS_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config = default_config()
    config.backends["deepseek"].enabled = True
    save_config(config, path)

    result = runner.invoke(app, ["backend-default", "deepseek"])

    assert result.exit_code == 2
    assert "backend is not ready" in result.output
    assert set(load_config(path).routing.backend_by_tier.values()) == {"gpt"}
