import json
from unittest.mock import patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from agentroute.cli import app
from agentroute.config import SubscriptionProfileConfig, default_config, save_config
from agentroute.install import _CodexAppServer
from agentroute.profiles import probe_profile


@pytest.fixture
def native_account_server(tmp_path):
    binary = tmp_path / "codex-bin"
    binary.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

home = Path(os.environ["CODEX_HOME"])
args = sys.argv[1:]
config = home / "config.toml"
uses_openai = not config.exists() or 'model_provider = "openai"' in config.read_text()
for index, arg in enumerate(args[:-1]):
    if arg in ("-c", "--config") and args[index + 1].startswith("model_provider="):
        uses_openai = json.loads(args[index + 1].split("=", 1)[1]) == "openai"
(home / "probe-args.json").write_text(json.dumps(args))
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    result = {}
    if method == "account/read":
        signed_in = uses_openai and (home / "auth.json").exists()
        result = {
            "account": {"type": "chatgpt", "planType": "pro"} if signed_in else None,
            "requiresOpenaiAuth": uses_openai,
        }
    elif method == "account/rateLimits/read":
        result = {
            "accountId": "private-account-id",
            "rateLimits": {"limitId": "codex", "primary": {"usedPercent": 37}},
        }
    print(json.dumps({"id": request["id"], "result": result}), flush=True)
"""
    )
    binary.chmod(0o755)
    return binary


def account_home(tmp_path, *, signed_in=True):
    home = tmp_path / "credentials"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "agentroute-azure"\n')
    if signed_in:
        (home / "auth.json").write_text("{}")
    return home


@pytest.mark.parametrize("name", ["default", "work"])
def test_gpt_probe_reads_login_despite_api_provider(tmp_path, native_account_server, name):
    home = account_home(tmp_path)
    config = default_config()
    config.capacity.enabled = True
    profile = SubscriptionProfileConfig(credential_home=str(home))

    result = probe_profile(name, profile, config, codex_binary=native_account_server)

    assert result.authenticated
    assert result.capacity.used_percent == 37
    assert result.capacity.status == "healthy"
    assert (home / "config.toml").read_text() == 'model_provider = "agentroute-azure"\n'
    assert (home / "auth.json").read_text() == "{}"
    assert "private-account-id" not in json.dumps(result.as_dict())


def test_gpt_probe_keeps_genuinely_signed_out_account_unavailable(
    tmp_path, native_account_server
):
    home = account_home(tmp_path, signed_in=False)
    result = probe_profile(
        "default",
        SubscriptionProfileConfig(credential_home=str(home)),
        default_config(),
        codex_binary=native_account_server,
    )

    assert not result.authenticated
    assert result.capacity.status == "unavailable"


def test_generic_helper_retains_configured_provider(tmp_path, native_account_server):
    home = account_home(tmp_path)
    with _CodexAppServer(native_account_server, codex_home=home) as client:
        result = client.request("account/read", {"refreshToken": False})

    assert result == {"account": None, "requiresOpenaiAuth": False}
    assert json.loads((home / "probe-args.json").read_text()) == ["app-server", "--stdio"]


def test_runtime_probe_preserves_disabled_capacity(tmp_path, native_account_server):
    home = account_home(tmp_path)
    config = default_config()
    result = probe_profile(
        "default",
        SubscriptionProfileConfig(credential_home=str(home)),
        config,
        codex_binary=native_account_server,
    )

    assert result.authenticated
    assert result.capacity.status == "disabled"
    assert not config.capacity.enabled


def test_account_list_reports_quota_without_enabling_switching(
    tmp_path, monkeypatch, native_account_server
):
    home = account_home(tmp_path)
    config = default_config()
    config.capacity.enabled = False
    config.capacity.profiles["default"] = SubscriptionProfileConfig(credential_home=str(home))
    path = tmp_path / "agentroute.yaml"
    save_config(config, path)
    saved_config = path.read_bytes()
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(path))
    monkeypatch.setenv("AGENTROUTE_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("agentroute.profiles.routed_codex_binary", lambda: native_account_server)
    with (
        patch(
            "agentroute.cli.profile_status",
            return_value={
                "name": "default",
                "active": False,
                "limits": [],
                "identity_status": "unverified",
                "status": "unavailable",
                "error": None,
            },
        ),
        patch("agentroute.cli.console", Console(width=180)),
    ):
        result = CliRunner().invoke(app, ["account", "list", "--json"])
        overview = CliRunner().invoke(app, ["account", "list"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert not payload["enabled"]
    assert payload["profiles"][0]["authenticated"]
    assert payload["profiles"][0]["capacity"]["used_percent"] == 37
    gpt = next(row for row in payload["backends"] if row["name"] == "gpt")
    assert gpt["status"] == "healthy"
    assert gpt["remaining_percent"] == 63
    assert "private-account-id" not in result.output
    assert overview.exit_code == 0, overview.output
    assert "Capacity management: disabled" in overview.output
    assert "37% used / 63% remaining" in overview.output
    assert "profile is not signed in" not in overview.output
    assert path.read_bytes() == saved_config
