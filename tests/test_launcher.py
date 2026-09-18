from pathlib import Path

from agentroute.config import default_config
from agentroute.launcher import codex_argv, startup_route_args


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
