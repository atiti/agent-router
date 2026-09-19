from unittest.mock import patch

from typer.testing import CliRunner

from agentroute.cli import app
from agentroute.config import default_config, load_config, save_config

runner = CliRunner()


def test_launch_codex_forwards_subcommands_and_arguments():
    with patch("agentroute.cli.launch_codex") as launch:
        result = runner.invoke(
            app,
            ["launch-codex", "--binary", "/tmp/codex-bin", "--", "exec", "hello"],
        )

    assert result.exit_code == 0
    launch.assert_called_once()
    assert str(launch.call_args.args[0]) == "/tmp/codex-bin"
    assert launch.call_args.args[1] == ["exec", "hello"]


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


def test_capacity_commands_persist_guardrails_and_keep_profiles_isolated(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    profile_home = tmp_path / "codex-personal"
    profile_home.mkdir()
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(path))
    config = default_config()
    save_config(config, path)

    assert runner.invoke(app, ["capacity", "enable"]).exit_code == 0
    assert runner.invoke(app, ["capacity", "budget", "azure", "--daily", "12"]).exit_code == 0
    assert runner.invoke(app, ["capacity", "fallback", "gpt", "azure"]).exit_code == 0
    added = runner.invoke(
        app,
        ["capacity", "profile-add", "personal", str(profile_home), "--select"],
    )

    updated = load_config(path)
    assert added.exit_code == 0
    assert updated.capacity.enabled is True
    assert updated.backends["azure"].daily_budget_usd == 12
    assert updated.backends["gpt"].fallback_backend == "azure"
    assert updated.capacity.active_profile == "personal"
    assert updated.capacity.profiles["personal"].codex_home == str(profile_home)
