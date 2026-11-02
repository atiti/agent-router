from types import SimpleNamespace

import pytest

from agentroute.launcher import browser_auth_helper_launch, launch_codex


@pytest.fixture
def browser_bundle(tmp_path, monkeypatch):
    root = tmp_path / "Example Routed.app" / "Contents" / "Resources"
    parent = root / "cua_node" / "bin" / "node_repl"
    parent.parent.mkdir(parents=True)
    parent.write_text("browser helper")
    (root / "agentroute-build-id").write_text("test-build")
    binary = root / "codex-bin"
    binary.write_text("native binary")
    monkeypatch.setattr("agentroute.launcher.sys.platform", "darwin")
    monkeypatch.setattr(
        "agentroute.launcher.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=str(parent) + "\n"),
    )
    return binary


def test_browser_auth_uses_existing_chatgpt_without_routing_work(browser_bundle, monkeypatch):
    def unexpected_config():
        pytest.fail("the authentication helper must not load the routing configuration")

    monkeypatch.setattr("agentroute.launcher.load_config", unexpected_config)
    monkeypatch.setenv("AGENTROUTE_LOCAL_SERVER_SOCKET", "/tmp/unrelated-owner")
    monkeypatch.setenv("AGENTROUTE_CONFIG", "/preserve/routing.yaml")
    captured = {}
    monkeypatch.setattr(
        "agentroute.launcher.os.execve",
        lambda path, argv, env: captured.update(path=path, argv=argv, env=env),
    )

    launch_codex(browser_bundle, ["app-server", "--listen", "stdio://"])

    assert captured["argv"] == [
        str(browser_bundle), "-c", 'model_provider="openai"',
        "app-server", "--listen", "stdio://",
    ]
    assert "AGENTROUTE_LOCAL_SERVER_SOCKET" not in captured["env"]
    assert captured["env"]["AGENTROUTE_CONFIG"] == "/preserve/routing.yaml"


@pytest.mark.parametrize("args", [
    [], ["exec", "hello"], ["resume", "latest"], ["app-server"],
    ["app-server", "--listen", "stdio://", "--analytics-default-enabled"],
    ["app-server", "--listen", "tcp://127.0.0.1:1234"],
    ["-c", "model_provider=custom", "app-server", "--listen", "stdio://"],
])
def test_ordinary_launches_do_not_match_browser_auth(browser_bundle, args):
    assert not browser_auth_helper_launch(browser_bundle, args)


def test_same_command_from_another_parent_stays_routed(browser_bundle, monkeypatch):
    monkeypatch.setattr(
        "agentroute.launcher.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="/other/Resources/cua_node/bin/node_repl\n"),
    )
    assert not browser_auth_helper_launch(browser_bundle, ["app-server", "--listen", "stdio://"])


def test_parent_lookup_failure_stays_routed(browser_bundle, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr("agentroute.launcher.subprocess.run", fail)
    assert not browser_auth_helper_launch(browser_bundle, ["app-server", "--listen", "stdio://"])


def test_unmarked_binary_stays_routed(browser_bundle):
    (browser_bundle.parent / "agentroute-build-id").unlink()
    assert not browser_auth_helper_launch(browser_bundle, ["app-server", "--listen", "stdio://"])


def test_non_macos_launch_stays_routed(browser_bundle, monkeypatch):
    monkeypatch.setattr("agentroute.launcher.sys.platform", "linux")
    assert not browser_auth_helper_launch(browser_bundle, ["app-server", "--listen", "stdio://"])
