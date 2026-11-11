from typer.testing import CliRunner

from agentroute.cli import app
from agentroute.config import default_config, load_config, save_config


def test_subscription_selection_preserves_jev_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(tmp_path / "config.yaml"))
    config = default_config()
    config.routing.classifier.engine = "jev"
    save_config(config)
    runner = CliRunner()
    result = runner.invoke(app, ["classifier-subscription-enable", "--model", "gpt-6-luna"])
    assert result.exit_code == 0, result.output
    settings = load_config().routing.classifier
    assert settings.engine == "jev"
    assert settings.transport == "codex_subscription"
    assert settings.model == "gpt-6-luna"
    assert settings.backend == "gpt"
    assert settings.timeout_seconds == 20
    assert settings.jev_shadow.llm_fallback_enabled
    result = runner.invoke(app, ["classifier-status"])
    assert "codex_subscription" in result.output
    assert "gpt-6-luna" in result.output


def test_subscription_rejects_unconfigured_backend_without_writing(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(path))
    save_config(default_config())
    prior = path.read_text()
    result = CliRunner().invoke(app, ["classifier-subscription-enable", "--backend", "missing"])
    assert result.exit_code != 0
    assert path.read_text() == prior


def test_api_enable_resets_subscription_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(tmp_path / "config.yaml"))
    config = default_config()
    config.routing.classifier.transport = "codex_subscription"
    save_config(config)
    result = CliRunner().invoke(
        app, ["classifier-enable", "--endpoint", "http://127.0.0.1:8081/v1/chat/completions"]
    )
    assert result.exit_code == 0, result.output
    assert load_config().routing.classifier.transport == "openai_compatible"


def test_subscription_status_does_not_read_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(tmp_path / "config.yaml"))
    config = default_config()
    save_config(config)
    runner = CliRunner()
    assert runner.invoke(app, ["classifier-subscription-enable"]).exit_code == 0
    result = runner.invoke(app, ["classifier-status"])
    assert result.exit_code == 0, result.output
    assert "ChatGPT subscription login" in result.output
    assert "AGENTROUTE_CLASSIFIER_API_KEY" not in result.output


def test_subscription_accepts_claude_and_grok_when_listed(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(tmp_path / "config.yaml"))
    config = default_config()
    config.routing.provider_selection.subscription_backends = ["gpt", "claude", "grok"]
    for name in ("claude", "grok"):
        config.backends[name] = config.backends["gpt"].model_copy(update={"enabled": True})
    save_config(config)
    for backend in ("claude", "grok"):
        result = CliRunner().invoke(
            app, ["classifier-subscription-enable", "--backend", backend, "--model", "m"]
        )
        assert result.exit_code == 0, result.output
        assert load_config().routing.classifier.backend == backend


def test_subscription_rejects_metered_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(tmp_path / "config.yaml"))
    config = default_config()
    save_config(config)
    prior = (tmp_path / "config.yaml").read_text()
    result = CliRunner().invoke(app, ["classifier-subscription-enable", "--backend", "deepseek"])
    assert result.exit_code != 0
    assert (tmp_path / "config.yaml").read_text() == prior


def test_jev_status_reports_own_credential_and_subscription_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(tmp_path / "config.yaml"))
    config = default_config()
    config.routing.classifier.engine = "jev"
    config.routing.classifier.transport = "codex_subscription"
    config.routing.classifier.jev_shadow.endpoint = "https://api.typesafe.ai/v1/systemone"
    config.routing.classifier.jev_shadow.api_key_env = "JEV_TEST_KEY"
    monkeypatch.setenv("JEV_TEST_KEY", "test-key")
    save_config(config)
    result = CliRunner().invoke(app, ["classifier-status"])
    assert result.exit_code == 0, result.output
    assert "Credential JEV_TEST_KEY: available" in result.output
    assert "LLM fallback credential: ChatGPT subscription login" in result.output


def test_bridge_status_does_not_claim_chatgpt_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(tmp_path / "config.yaml"))
    config = default_config()
    config.routing.classifier.transport = "codex_subscription"
    config.routing.classifier.backend = "claude"
    save_config(config)
    result = CliRunner().invoke(app, ["classifier-status"])
    assert result.exit_code == 0, result.output
    assert "claude selected backend credentials" in result.output
    assert "ChatGPT subscription login" not in result.output
