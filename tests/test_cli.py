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


def test_backend_add_creates_credentialless_ollama_backend(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    codex_config = tmp_path / "codex" / "config.toml"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(config_path))
    monkeypatch.setenv("CODEX_HOME", str(codex_config.parent))

    result = runner.invoke(
        app,
        [
            "backend-add",
            "ollama",
            "--base-url",
            "http://127.0.0.1:11434/v1",
            "--model",
            "qwen3-coder:30b",
            "--display-name",
            "Local Ollama",
        ],
    )

    assert result.exit_code == 0, result.output
    config = load_config(config_path)
    backend = config.backends["ollama"]
    assert backend.enabled
    assert backend.api_key_env is None
    assert backend.codex_provider == "agentroute-ollama"
    assert backend.tiers["smart"].model == "qwen3-coder:30b"
    assert backend.tool_compatibility == "functions_and_apply_patch"
    rendered = codex_config.read_text(encoding="utf-8")
    assert "[model_providers.agentroute-ollama]" in rendered
    assert "Use it with `agentroute backend-route fast ollama`" in result.output


def test_backend_add_supports_custom_tiers_and_api_key(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(config_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    result = runner.invoke(
        app,
        [
            "backend-add",
            "private-gateway",
            "--base-url",
            "https://models.example.test/v1",
            "--model",
            "default-model",
            "--smart-model",
            "reasoning-model",
            "--api-key-env",
            "PRIVATE_GATEWAY_KEY",
            "--tool-compatibility",
            "full",
        ],
    )

    assert result.exit_code == 0, result.output
    backend = load_config(config_path).backends["private-gateway"]
    assert backend.tiers["smart"].model == "reasoning-model"
    assert backend.api_key_env == "PRIVATE_GATEWAY_KEY"
    assert backend.tool_compatibility == "full"
    assert "backend-credential-import" in result.output
    assert "private-gateway SOURCE_ENV" in result.output


def test_backend_add_rejects_non_responses_url(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(tmp_path / "config.yaml"))

    result = runner.invoke(
        app,
        ["backend-add", "ollama", "--base-url", "http://127.0.0.1:11434", "--model", "qwen"],
    )

    assert result.exit_code == 2
    assert "must end in /v1" in result.output


def test_backend_add_rejects_routing_directive_name(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(tmp_path / "config.yaml"))

    result = runner.invoke(
        app,
        ["backend-add", "auto", "--base-url", "http://127.0.0.1:11434/v1", "--model", "qwen"],
    )

    assert result.exit_code == 2
    assert "reserved for routing directives" in result.output


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


def test_capacity_profile_model_sets_and_clears_override(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(path))
    config = default_config()
    config.capacity.profiles["personal"] = SubscriptionProfileConfig(
        codex_home=str(tmp_path / "personal")
    )
    save_config(config, path)

    set_result = runner.invoke(
        app,
        [
            "capacity",
            "profile-model",
            "personal",
            "max",
            "gpt-6-astra",
            "--reasoning-effort",
            "high",
        ],
    )

    assert set_result.exit_code == 0
    target = load_config(path).capacity.profiles["personal"].tiers["max"]
    assert target.model == "gpt-6-astra"
    assert target.reasoning_effort == "high"

    clear_result = runner.invoke(
        app, ["capacity", "profile-model", "personal", "max", "--clear"]
    )

    assert clear_result.exit_code == 0
    assert "max" not in load_config(path).capacity.profiles["personal"].tiers


def test_profile_bootstrap_copies_setup_but_not_authentication_or_sessions(tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "config.toml").write_text('model = "gpt"\n', encoding="utf-8")
    (source / "hooks.json").write_text('{"hooks": {}}\n', encoding="utf-8")
    (source / "AGENTS.md").write_text("shared instructions\n", encoding="utf-8")
    (source / "skills" / "shared").mkdir(parents=True)
    (source / "skills" / "shared" / "SKILL.md").write_text("skill\n", encoding="utf-8")
    (source / "rules").mkdir()
    (source / "rules" / "shared.md").write_text("rule\n", encoding="utf-8")
    (destination / "auth.json").write_text("profile credential\n", encoding="utf-8")
    (destination / "sessions").mkdir()
    (destination / "sessions" / "thread.jsonl").write_text("thread\n", encoding="utf-8")

    path = tmp_path / "config.yaml"
    config = default_config()
    config.capacity.profiles["work"] = SubscriptionProfileConfig(codex_home=str(destination))
    save_config(config, path)
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(path))
    monkeypatch.setattr("agentroute.cli.sync_codex_providers", lambda _config, value: (value, None))
    monkeypatch.setattr("agentroute.cli.merge_codex_hook", lambda value: (value, None))
    monkeypatch.setattr("agentroute.cli.trust_agentroute_hooks", lambda *_args, **_kwargs: 3)

    result = runner.invoke(app, ["capacity", "profile-bootstrap", "work", "--from", str(source)])

    assert result.exit_code == 0
    assert (destination / "config.toml").read_text(encoding="utf-8") == 'model = "gpt"\n'
    assert (destination / "skills" / "shared" / "SKILL.md").is_file()
    assert (destination / "rules" / "shared.md").is_file()
    assert (destination / "auth.json").read_text(encoding="utf-8") == "profile credential\n"
    assert (destination / "sessions" / "thread.jsonl").read_text(encoding="utf-8") == "thread\n"


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
