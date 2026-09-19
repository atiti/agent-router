from pathlib import Path

from agentroute.config import SubscriptionProfileConfig, default_config
from agentroute.launcher import codex_argv, launch_codex, startup_route_args


def test_startup_uses_normal_tier_backend_before_first_turn():
    config = default_config()
    config.backends["azure"].enabled = True
    for tier in ("fast", "normal", "smart", "max"):
        config.routing.backend_by_tier[tier] = "azure"

    args = startup_route_args(config, [])

    assert 'model_provider="agentroute-azure"' in args
    assert "gpt-5" in args
    assert 'model_reasoning_effort="medium"' in args


def test_explicit_provider_override_has_precedence_over_startup_default():
    config = default_config()
    config.backends["azure"].enabled = True
    config.routing.backend_by_tier["normal"] = "azure"

    assert startup_route_args(config, ["-c", "model_provider=openai"]) == []


def test_explicit_model_keeps_default_provider_but_not_default_model():
    config = default_config()
    config.backends["azure"].enabled = True
    config.routing.backend_by_tier["normal"] = "azure"

    args = codex_argv(Path("/tmp/codex-bin"), ["--model", "custom"], config)

    assert 'model_provider="agentroute-azure"' in args
    assert args.count("--model") == 1
    assert args[-2:] == ["--model", "custom"]


def test_launch_profile_sets_isolated_codex_home_before_exec(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    binary = tmp_path / "codex-bin"
    binary.write_text("placeholder")
    config = default_config()
    config.capacity.profiles["personal"] = SubscriptionProfileConfig(
        codex_home=str(profile_home), priority=1
    )
    from agentroute.config import save_config

    save_config(config, config_path)
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(config_path))

    captured = {}

    def fake_execve(path, argv, environment):
        captured.update(path=path, argv=argv, environment=environment)
        raise SystemExit

    monkeypatch.setattr("agentroute.launcher.os.execve", fake_execve)
    monkeypatch.setattr(
        "agentroute.launcher.select_launch_profile",
        lambda *_args, **_kwargs: ("personal", None, ()),
    )

    try:
        launch_codex(binary, ["--version"], "personal")
    except SystemExit:
        pass

    assert captured["environment"]["CODEX_HOME"] == str(profile_home)
