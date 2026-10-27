from pathlib import Path

import pytest

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


def test_launch_profile_preserves_canonical_codex_home_before_exec(tmp_path, monkeypatch):
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
    canonical_home = tmp_path / "canonical"
    monkeypatch.setenv("CODEX_HOME", str(canonical_home))

    try:
        launch_codex(binary, ["--version"], "personal")
    except SystemExit:
        pass

    assert captured["environment"]["CODEX_HOME"] == str(canonical_home)


def test_launch_enables_native_traces_and_preserves_explicit_trace_root(tmp_path, monkeypatch):
    from agentroute.profiling import set_capture

    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_ROLLOUT_TRACE_ROOT", raising=False)
    monkeypatch.setattr("agentroute.launcher.load_config", default_config)
    captured = {}

    def fake_exec(path, argv, environment):
        captured.update(environment)

    monkeypatch.setattr("agentroute.launcher.os.execve", fake_exec)
    binary = tmp_path / "codex"
    launch_codex(binary, [])
    assert "CODEX_ROLLOUT_TRACE_ROOT" not in captured
    set_capture(True)
    launch_codex(binary, [])
    assert captured["CODEX_ROLLOUT_TRACE_ROOT"] == str(tmp_path / "profiling" / "traces")
    assert (tmp_path / "profiling" / "traces").stat().st_mode & 0o777 == 0o700
    monkeypatch.setenv("CODEX_ROLLOUT_TRACE_ROOT", "/custom/traces")
    launch_codex(binary, [])
    assert captured["CODEX_ROLLOUT_TRACE_ROOT"] == "/custom/traces"


def test_launch_refreshes_managed_catalog_before_native_start(tmp_path, monkeypatch):
    monkeypatch.setattr("agentroute.launcher.load_config", default_config)
    catalog = tmp_path / 'path with spaces' / 'config.agentroute-models.json'
    calls = []
    monkeypatch.setattr("agentroute.providers.refresh_launch_catalog", lambda config: (
        calls.append("refresh") or catalog
    ))
    monkeypatch.setattr("agentroute.launcher.os.execve", lambda path, argv, env: calls.append(argv))
    launch_codex(tmp_path / "codex", ["exec", "hello"])
    assert calls[0] == "refresh"
    assert f'model_catalog_json="{catalog}"' in calls[1]
    assert calls[1][-2:] == ["exec", "hello"]

    for args in [["--version"], ["--remote", "unix:///owner"],
                 ["-c", 'model_catalog_json="/custom/models.json"', "exec", "hello"],
                 ["-p", "custom", "exec", "hello"]]:
        calls.clear()
        launch_codex(tmp_path / "codex", args)
        assert len(calls) == 1
        assert calls[0][-len(args):] == args


def test_launch_config_selects_new_defaults_and_leaves_hook_config_unchanged(tmp_path, monkeypatch):
    from agentroute.config import ModelTarget, load_config, save_config

    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path))
    monkeypatch.delenv("AGENTROUTE_CONFIG", raising=False)
    old = default_config()
    old.backends["gpt"].tiers["normal"] = ModelTarget(model="gpt-6.1-sol")
    old.launch_config = "new.yaml"
    new = default_config()
    new.backends["gpt"].tiers["normal"] = ModelTarget(model="gpt-6.1-terra")
    save_config(old, tmp_path / "config.yaml")
    save_config(new, tmp_path / "new.yaml")
    captured = {}
    refreshed = []
    monkeypatch.setattr("agentroute.launcher.os.execve", lambda path, argv, env:
                        captured.update(argv=argv, env=env))
    monkeypatch.setattr("agentroute.classifier_refresh.refresh_classifier_catalog",
                        lambda config, path: refreshed.append(path))
    launch_codex(tmp_path / "codex", [])
    assert "gpt-6.1-terra" in captured["argv"]
    assert captured["env"]["AGENTROUTE_CONFIG"] == str(tmp_path / "new.yaml")
    assert refreshed == [tmp_path / "new.yaml"]
    assert load_config().backends["gpt"].tiers["normal"].model == "gpt-6.1-sol"


def test_explicit_config_bypasses_launch_redirect(tmp_path, monkeypatch):
    from agentroute.config import save_config

    config = default_config()
    config.launch_config = "missing.yaml"
    path = tmp_path / "explicit.yaml"
    save_config(config, path)
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(path))
    captured = {}
    monkeypatch.setattr("agentroute.launcher.os.execve", lambda p, argv, env:
                        captured.update(env=env))
    launch_codex(tmp_path / "codex", [])
    assert captured["env"]["AGENTROUTE_CONFIG"] == str(path)


def test_missing_or_chained_launch_config_fails_before_exec(tmp_path, monkeypatch):
    from agentroute.config import save_config

    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path))
    monkeypatch.delenv("AGENTROUTE_CONFIG", raising=False)
    config = default_config()
    config.launch_config = "missing.yaml"
    save_config(config, tmp_path / "config.yaml")
    with pytest.raises(FileNotFoundError):
        launch_codex(tmp_path / "codex", [])
    config.launch_config = "config.yaml"
    save_config(config, tmp_path / "config.yaml")
    with pytest.raises(ValueError, match="redirect chain"):
        launch_codex(tmp_path / "codex", [])


def test_launch_config_classifier_failure_keeps_native_launch_fail_open(
    tmp_path, monkeypatch, capsys
):
    from agentroute.config import save_config

    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path))
    monkeypatch.delenv("AGENTROUTE_CONFIG", raising=False)
    base = default_config()
    base.launch_config = "selected.yaml"
    save_config(base, tmp_path / "config.yaml")
    save_config(default_config(), tmp_path / "selected.yaml")

    def fail_refresh(config, path):
        raise RuntimeError("classifier credential unavailable")

    monkeypatch.setattr("agentroute.classifier_refresh.refresh_classifier_catalog", fail_refresh)
    launched = []
    monkeypatch.setattr("agentroute.launcher.os.execve", lambda p, argv, env: launched.append(argv))
    launch_codex(tmp_path / "codex", [])
    assert len(launched) == 1
    assert "classifier credential unavailable" in capsys.readouterr().err
