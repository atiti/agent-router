import json
from unittest.mock import call, patch

from rich.text import Text
from typer.testing import CliRunner

from agentroute.capacity import CapacityState
from agentroute.cli import app
from agentroute.config import (
    ClaudeSubscriptionProfile,
    SubscriptionProfileConfig,
    default_config,
    load_config,
    save_config,
)
from agentroute.profiles import ProfileStatus
from agentroute.providers import ensure_claude_bridge_backend

runner = CliRunner()


def test_help_lists_prompt_tags(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(tmp_path / "config.yaml"))

    result = runner.invoke(app, ["help"])

    assert result.exit_code == 0
    assert "AgentRoute prompt tags" in result.output
    assert "Tier: @fast @normal @smart @max @auto" in result.output
    assert "Reasoning: @none @minimal @low @medium @high @xhigh @ultra @persistent" in result.output


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


def test_default_account_uses_canonical_auth_and_named_account_uses_credential_slot(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.yaml"
    canonical = tmp_path / "canonical"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(config_path))
    monkeypatch.setenv("CODEX_HOME", str(canonical))

    result = runner.invoke(app, ["account", "add", "markster", "--select"])

    assert result.exit_code == 0, result.output
    config = load_config(config_path)
    assert config.capacity.active_profile == "markster"
    assert config.capacity.profiles["default"].credential_home is None
    assert config.capacity.profiles["markster"].credential_home == str(
        canonical / "accounts" / "markster"
    )
    assert "credentials only" in result.output


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


def test_profile_bootstrap_is_a_noop_when_configuration_is_shared(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    config = default_config()
    config.capacity.profiles["work"] = SubscriptionProfileConfig(
        credential_home=str(tmp_path / "accounts" / "work")
    )
    save_config(config, path)
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(path))

    result = runner.invoke(app, ["capacity", "profile-bootstrap", "work"])

    assert result.exit_code == 0
    assert "No bootstrap is needed" in result.output


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

    with (
        patch("agentroute.cli.probe_profiles", return_value=(profile,)),
        patch(
            "agentroute.cli.profile_status",
            return_value={
                "name": "default",
                "active": True,
                "status": "unavailable",
                "observed_at": None,
                "identity_status": "unverified",
                "limits": [],
                "error": "not signed in",
            },
        ),
    ):
        result = runner.invoke(app, ["capacity", "status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    gpt = next(item for item in payload["backends"] if item["name"] == "gpt")
    assert gpt["status"] == "healthy"
    assert gpt["detail"] == "subscription 62% used / 38% remaining"
    assert payload["profiles"][0]["account_hash"] == "hashed-account"
    assert "account_id" not in payload["profiles"][0]["capacity"]
    assert "raw-account-id" not in result.output


def test_capacity_status_shows_live_claude_subscription_usage(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(path))
    monkeypatch.setenv("AGENTROUTE_DATA_DIR", str(tmp_path))
    config = default_config()
    ensure_claude_bridge_backend(config, 8090)
    config.claude_subscriptions.profiles["second"] = ClaudeSubscriptionProfile(
        config_dir=str(tmp_path / "claude-accounts" / "second"), priority=1
    )
    save_config(config, path)
    default_profile = {
        "name": "default",
        "active": True,
        "status": "live",
        "observed_at": "2026-09-26T21:00:00Z",
        "identity_status": "verified",
        "limits": [
            {
                "kind": "session",
                "label": "5-hour session",
                "percent": 42,
                "resets_at": "2026-09-26T22:00:00Z",
                "is_active": True,
            }
        ],
        "error": None,
    }
    second_profile = {
        **default_profile,
        "name": "second",
        "active": False,
        "status": "recorded",
        "limits": [
            {
                "kind": "session",
                "label": "5-hour session",
                "percent": 18,
                "resets_at": "2026-09-26T23:00:00Z",
                "is_active": True,
            }
        ],
    }

    gpt_profile = ProfileStatus(
        name="default",
        codex_home=str(tmp_path / "codex"),
        priority=0,
        enabled=True,
        authenticated=True,
        account_hash="masked-id",
        capacity=CapacityState("gpt", "healthy", "subscription 20% used", "subscription"),
    )
    with (
        patch("agentroute.cli.profile_status", side_effect=[default_profile, second_profile])
        as profile_read,
        patch("agentroute.cli.probe_profiles", return_value=(gpt_profile,)),
    ):
        result = runner.invoke(app, ["capacity", "status", "--json"])

    assert result.exit_code == 0, result.output
    assert profile_read.call_args_list == [
        call(config, "default", offline=False),
        call(config, "second", offline=False),
    ]
    usage = json.loads(result.output)["claude_subscription"]
    assert usage["status"] == "live"
    assert usage["limits"][0]["used_percent"] == 42.0
    assert usage["limits"][0]["remaining_percent"] == 58.0
    assert usage["limits"][0]["reset_local"]

    profile_read.side_effect = [default_profile, second_profile]
    overview = runner.invoke(app, ["capacity", "status"])
    assert overview.exit_code == 0, overview.output
    assert "GPT subscription profiles" in overview.output
    assert "Claude subscription profiles" in overview.output
    assert "Claude subscription usage" not in overview.output
    assert "default" in overview.output and "second" in overview.output
    assert "Account hash" in overview.output


def test_capacity_status_no_probe_uses_the_recorded_claude_sample(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(path))
    monkeypatch.setenv("AGENTROUTE_DATA_DIR", str(tmp_path))
    config = default_config()
    ensure_claude_bridge_backend(config, 8090)
    save_config(config, path)
    profile = {
        "name": "default",
        "active": True,
        "status": "recorded",
        "observed_at": "2026-09-27T05:45:25Z",
        "identity_status": "verified",
        "limits": [{
            "kind": "session",
            "label": "5-hour session",
            "percent": 79,
            "resets_at": 1790460000,
            "is_active": True,
        }],
        "error": None,
    }

    with patch("agentroute.cli.profile_status", return_value=profile) as profile_read:
        result = runner.invoke(app, ["capacity", "status", "--json", "--no-probe"])

    assert result.exit_code == 0, result.output
    profile_read.assert_called_once_with(config, "default", offline=True)
    usage = json.loads(result.output)["claude_subscription"]
    assert usage["status"] == "recorded"
    assert usage["limits"][0]["used_percent"] == 79.0
    assert usage["observed_at_local"]



def test_hosted_jev_enable_verifies_catalog_and_preserves_llm_fallback(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(path))
    config = default_config()
    config.routing.classifier.endpoint = "http://127.0.0.1:8081/v1/chat/completions"
    config.routing.classifier.model = "qwen-fallback"
    save_config(config, path)
    with patch("agentroute.classifier.JevShadowClassifier.verify_catalog", return_value=(
        ["jev-latest"], "catalog-hash", "2026-09-28T00:00:00+00:00",
    )):
        result = runner.invoke(app, [
            "classifier-jev-enable", "--endpoint", "https://api.typesafe.ai/v1/systemone",
            "--model", "jev-latest", "--api-key-file", str(tmp_path / "key"),
            "--allow-remote", "--timeout", "5",
        ])
    assert result.exit_code == 0, result.output
    settings = load_config(path).routing.classifier
    assert settings.engine == "jev" and settings.enabled
    assert settings.jev_shadow.allow_remote
    assert settings.jev_shadow.catalog_models == ["jev-latest"]
    assert settings.model == "qwen-fallback"
    assert settings.endpoint == "http://127.0.0.1:8081/v1/chat/completions"
    assert settings.api_key_file is None


def test_hosted_jev_enable_requires_explicit_egress_and_keeps_config(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(path))
    save_config(default_config(), path)
    before = path.read_bytes()
    result = runner.invoke(app, [
        "classifier-jev-enable", "--endpoint", "https://api.typesafe.ai/v1/systemone",
        "--model", "jev-latest",
    ])
    assert result.exit_code == 2
    assert "--allow-remote" in Text.from_ansi(result.output).plain
    assert path.read_bytes() == before


def test_jev_catalog_verify_and_refresh_use_active_endpoint(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(path))
    config = default_config()
    config.routing.classifier.enabled = True
    config.routing.classifier.engine = "jev"
    config.routing.classifier.jev_shadow.endpoint = "https://api.typesafe.ai/v1/systemone"
    config.routing.classifier.jev_shadow.model = "jev-latest"
    config.routing.classifier.jev_shadow.allow_remote = True
    save_config(config, path)
    with patch("agentroute.classifier.JevShadowClassifier.verify_catalog", return_value=(
        ["jev-latest"], "digest", "2026-09-28T00:00:00+00:00",
    )) as verify:
        result = runner.invoke(app, ["classifier-refresh"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["classifier-verify"])
        assert result.exit_code == 0, result.output
    assert verify.call_count == 2
    assert load_config(path).routing.classifier.jev_shadow.catalog_hash == "digest"
    assert load_config(path).routing.classifier.catalog_hash is None
