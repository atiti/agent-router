import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from agentroute.claude_bridge import CredentialError
from agentroute.claude_profiles import (
    ProfileCredential,
    profile_account_identity,
    profile_scope,
    profile_status,
    profile_usage_path,
)
from agentroute.cli import app
from agentroute.config import ClaudeSubscriptionProfile, default_config, load_config

runner = CliRunner()


def test_profile_credential_uses_a_separate_keychain_namespace(tmp_path):
    path = tmp_path / "second"
    profile = ClaudeSubscriptionProfile(config_dir=str(path), auth_generation="login-1")

    credential = ProfileCredential("second", profile)

    suffix = hashlib.sha256(str(path).encode()).hexdigest()[:8]
    assert credential.directory == path
    assert credential.keychain_service.endswith(f"-{suffix}")
    assert credential.profile_scope.startswith("second:login-1:")


def test_profile_identity_tracks_account_not_rotating_tokens(tmp_path):
    directory = tmp_path / "second"
    directory.mkdir()
    metadata_path = directory / ".claude.json"
    profile = ClaudeSubscriptionProfile(config_dir=str(directory), auth_generation="login-1")
    metadata = {
        "oauthAccount": {"accountUuid": "account-a", "organizationUuid": "organization-a"},
        "oauth": {"accessToken": "token-one", "expiresAt": 100},
    }
    metadata_path.write_text(json.dumps(metadata))
    account_a = profile_account_identity(profile)
    scope_a = profile_scope("second", profile, account_a)

    metadata["oauth"]["accessToken"] = "token-rotated"
    metadata["oauth"]["expiresAt"] = 200
    metadata_path.write_text(json.dumps(metadata))
    assert profile_account_identity(profile) == account_a
    assert profile_scope("second", profile) == scope_a

    metadata["oauthAccount"]["accountUuid"] = "account-b"
    metadata_path.write_text(json.dumps(metadata))
    assert profile_account_identity(profile) != account_a
    assert profile_scope("second", profile) != scope_a


def test_missing_profile_identity_is_reported_as_unverified(tmp_path):
    profile = ClaudeSubscriptionProfile(config_dir=str(tmp_path / "unsigned"))
    config = default_config()
    config.claude_subscriptions.profiles["second"] = profile

    row = profile_status(config, "second", offline=True)

    assert row["identity_status"] == "unverified"
    assert row["status"] == "unverified"
    assert "before routing" in row["error"]


def test_live_profile_status_persists_separate_quotas(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path / "agentroute"))
    config = default_config()
    config.claude_subscriptions.profiles["first"] = ClaudeSubscriptionProfile(
        config_dir=str(tmp_path / "claude" / "first"), auth_generation="first-login", priority=1
    )
    config.claude_subscriptions.profiles["second"] = ClaudeSubscriptionProfile(
        config_dir=str(tmp_path / "claude" / "second"), auth_generation="second-login", priority=2
    )
    config.claude_subscriptions.profiles["default"].enabled = False
    config.claude_subscriptions.active_profile = "first"
    for name, account in (("first", "account-a"), ("second", "account-b")):
        profile = config.claude_subscriptions.profiles[name]
        directory = Path(profile.config_dir)
        directory.mkdir(parents=True)
        (directory / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "accountUuid": account,
                        "organizationUuid": "organization",
                    }
                }
            )
        )

    calls = []
    quotas = {"first": 100, "second": 40}
    monkeypatch.setattr(
        "agentroute.claude_profiles.profile_credential",
        lambda _config, name: SimpleNamespace(profile_name=name),
    )

    def fetch(credential, *, refresh, timeout):
        calls.append((credential.profile_name, refresh, timeout))
        return {
            "limits": [
                {
                    "kind": "session",
                    "percent": quotas[credential.profile_name],
                    "resets_at": time.time() + 3600,
                }
            ]
        }

    monkeypatch.setattr("agentroute.claude_profiles.fetch_subscription_usage", fetch)
    rows = {name: profile_status(config, name) for name in ("first", "second")}

    assert calls == [("first", False, 5), ("second", False, 5)]
    assert all(row["status"] == "live" for row in rows.values())
    for name in ("first", "second"):
        profile = config.claude_subscriptions.profiles[name]
        identity = profile_account_identity(profile)
        state = json.loads(profile_usage_path(name, identity, profile.auth_generation).read_text())
        assert state["snapshot"]["windows"]["five_hour"]["used_percent"] == quotas[name]


def test_profile_credentials_stay_in_private_non_darwin_files(tmp_path, monkeypatch):
    monkeypatch.setattr("agentroute.claude_profiles.platform.system", lambda: "Linux")
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir(mode=0o700)
    second_dir.mkdir(mode=0o700)
    first_file = first_dir / ".credentials.json"
    second_file = second_dir / ".credentials.json"
    first_file.write_text('{"claudeAiOauth":{"refreshToken":"first"}}')
    second_file.write_text('{"claudeAiOauth":{"refreshToken":"second"}}')
    first_file.chmod(0o600)
    second_file.chmod(0o600)
    first = ProfileCredential("first", ClaudeSubscriptionProfile(config_dir=str(first_dir)))
    second = ProfileCredential("second", ClaudeSubscriptionProfile(config_dir=str(second_dir)))

    assert first._read_keychain()["claudeAiOauth"]["refreshToken"] == "first"
    assert second._read_keychain()["claudeAiOauth"]["refreshToken"] == "second"
    first._write_keychain({"claudeAiOauth": {"refreshToken": "first-refreshed"}})

    assert json.loads(first_file.read_text())["claudeAiOauth"]["refreshToken"] == "first-refreshed"
    assert json.loads(second_file.read_text())["claudeAiOauth"]["refreshToken"] == "second"
    assert first_file.stat().st_mode & 0o777 == 0o600

    symlink_target = tmp_path / "outside.json"
    symlink_target.write_text('{"claudeAiOauth":{"refreshToken":"outside"}}')
    symlink_target.chmod(0o600)
    first_file.unlink()
    first_file.symlink_to(symlink_target)
    with pytest.raises(CredentialError, match="must not be a symlink"):
        first._read_keychain()
    with pytest.raises(CredentialError, match="must not be a symlink"):
        first._write_keychain({"claudeAiOauth": {"refreshToken": "unsafe"}})


def test_profile_add_and_use_creates_private_separate_account_folder(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    account_home = tmp_path / "agentroute"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(config_path))
    monkeypatch.setenv("AGENTROUTE_HOME", str(account_home))

    added = runner.invoke(app, ["bridge", "profile", "add", "second"])
    selected = runner.invoke(app, ["bridge", "profile", "use", "second"])

    assert added.exit_code == 0, added.output
    assert selected.exit_code == 0, selected.output
    config = load_config(config_path)
    directory = Path(config.claude_subscriptions.profiles["second"].config_dir)
    assert directory == account_home / "claude-accounts" / "second"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert config.claude_subscriptions.active_profile == "second"


def test_profile_login_uses_claudeai_in_the_isolated_config_dir(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    account_home = tmp_path / "agentroute"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(config_path))
    monkeypatch.setenv("AGENTROUTE_HOME", str(account_home))
    runner.invoke(app, ["bridge", "profile", "add", "second"])
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR",
    ):
        monkeypatch.setenv(name, "must-not-reuse")
    monkeypatch.setattr("agentroute.claude_profile_cli.shutil.which", lambda _: "/bin/claude")
    monkeypatch.setattr(
        "agentroute.claude_profile_cli.profile_status", lambda *args: {"error": None}
    )
    with patch("agentroute.claude_profile_cli.subprocess.run") as run:
        run.return_value.returncode = 0
        result = runner.invoke(app, ["bridge", "profile", "login", "second"])

    assert result.exit_code == 0, result.output
    assert run.call_args.args[0] == ["/bin/claude", "auth", "login", "--claudeai"]
    launch_env = run.call_args.kwargs["env"]
    assert launch_env["CLAUDE_CONFIG_DIR"] == str(account_home / "claude-accounts" / "second")
    assert (
        not {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_SECURESTORAGE_CONFIG_DIR",
        }
        & launch_env.keys()
    )


def test_failed_profile_login_does_not_rotate_auth_generation(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(config_path))
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path / "agentroute"))
    runner.invoke(app, ["bridge", "profile", "add", "second"])
    original = load_config(config_path).claude_subscriptions.profiles["second"].auth_generation
    monkeypatch.setattr("agentroute.claude_profile_cli.shutil.which", lambda _: "/bin/claude")
    with patch("agentroute.claude_profile_cli.subprocess.run") as run:
        run.return_value.returncode = 1
        result = runner.invoke(app, ["bridge", "profile", "login", "second"])

    assert result.exit_code == 1
    assert (
        load_config(config_path).claude_subscriptions.profiles["second"].auth_generation == original
    )
