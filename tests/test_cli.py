import json
from unittest.mock import patch

from typer.testing import CliRunner

from agentroute.capacity import CapacityState
from agentroute.cli import app
from agentroute.config import (
    SubscriptionProfileConfig,
    default_config,
    load_config,
    save_config,
)
from agentroute.profiles import ProfileStatus

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


def test_capacity_status_uses_active_profile_telemetry_and_hides_raw_account_id(
    tmp_path, monkeypatch
):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(path))
    monkeypatch.setenv("AGENTROUTE_DATA_DIR", str(tmp_path))
    config = default_config()
    config.capacity.enabled = True
    config.capacity.active_profile = "personal"
    config.capacity.profiles["personal"] = SubscriptionProfileConfig(
        codex_home=str(tmp_path / "codex-personal"),
        priority=10,
    )
    save_config(config, path)
    profile = ProfileStatus(
        name="personal",
        codex_home=str(tmp_path / "codex-personal"),
        priority=10,
        enabled=True,
        authenticated=True,
        account_hash="hashed-account",
        capacity=CapacityState(
            backend="gpt",
            status="healthy",
            detail="subscription 62% used / 38% remaining",
            trigger="subscription",
            used_percent=62,
            resets_at=1790057790,
            account_id="raw-account-id",
        ),
    )

    with patch("agentroute.cli.probe_profiles", return_value=(profile,)):
        result = runner.invoke(app, ["capacity", "status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    gpt = next(item for item in payload["backends"] if item["name"] == "gpt")
    assert gpt["status"] == "healthy"
    assert gpt["detail"] == "subscription 62% used / 38% remaining"
    assert payload["profiles"][0]["account_hash"] == "hashed-account"
    assert "account_id" not in payload["profiles"][0]["capacity"]
    assert "raw-account-id" not in result.output
