import json
import os
import stat
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from typer.testing import CliRunner

from agentroute import browser_cli
from agentroute import browser_profiles as bp


@pytest.fixture
def chrome(tmp_path, monkeypatch):
    """A synthetic browser root: two real profiles plus stale and unsafe Local State keys."""
    root = tmp_path / "chrome-root"
    for name in ("Default", "Profile 3", "Profile 12"):
        (root / name).mkdir(parents=True)
        (root / name / "Preferences").write_text("{}")
    info_cache = {
        "Profile 12": {"name": "Second", "user_name": "second@example.test"},
        "Default": {"name": "Main", "user_name": "main@example.test"},
        "Profile 3": {"name": "Work", "user_name": ""},
        "Profile 9": {"name": "Deleted"},
        "../outside": {"name": "Escape"},
    }
    (root / "Local State").write_text(json.dumps({"profile": {"info_cache": info_cache}}))
    monkeypatch.setenv(bp.USER_DATA_ENV, str(root))
    return root


def test_profiles_lists_existing_safe_directories_without_email_by_default(chrome):
    found = bp.list_profiles()
    assert [(p.profile_dir, p.label, p.email) for p in found] == [
        ("Default", "Main", None),
        ("Profile 3", "Work", None),
        ("Profile 12", "Second", None),
    ]
    emails = {p.profile_dir: p.email for p in bp.list_profiles(include_email=True)}
    assert emails == {"Default": "main@example.test", "Profile 3": None,
                      "Profile 12": "second@example.test"}


def test_bind_writes_private_sidecar_and_keeps_mode_on_rebind(chrome):
    bp.bind("work", "Profile 3", sites=["example.test"])
    path = bp.sidecar_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert bp.load_bindings()["work"].mode == "browser"

    bp.set_mode("work", "both")
    rebound = bp.bind("work", "Profile 12")
    assert rebound.mode == "both"
    assert rebound.sites == ()
    assert json.loads(path.read_text())["bindings"]["work"]["profile_dir"] == "Profile 12"
    assert [p.name for p in path.parent.iterdir() if p.name.startswith(".browser-profiles.json.")
            and not p.name.endswith(".lock")] == []


@pytest.mark.parametrize(("alias", "profile_dir"), [
    ("work", "Profile 9"),          # listed in Local State but gone from disk
    ("work", "../outside"),
    ("work", "Profile 3/../Default"),
    ("work", "System Profile"),
    ("Work", "Profile 3"),
    ("../x", "Profile 3"),
])
def test_bind_rejects_missing_or_unsafe_targets(chrome, alias, profile_dir):
    with pytest.raises(bp.BrowserProfileError):
        bp.bind(alias, profile_dir)
    assert not bp.sidecar_path().exists()


def test_resolve_points_vendor_overrides_at_exact_profile_and_revalidates(chrome):
    bp.bind("main", "Default")
    resolved = bp.resolve_binding("main")
    assert resolved.env == {
        bp.USER_DATA_ENV: str(chrome),
        bp.PREFERENCES_ENV: str(chrome / "Default" / "Preferences"),
    }
    assert resolved.browser_allowed and not resolved.computer_allowed
    assert resolved.to_json()["launch_args"] == ["--profile-directory=Default"]

    (chrome / "Default" / "Preferences").unlink()
    with pytest.raises(bp.BrowserProfileError, match="does not exist"):
        bp.resolve_binding("main")
    with pytest.raises(bp.BrowserProfileError, match="no browser binding"):
        bp.resolve_binding("other")


def test_explicit_user_data_dir_is_stored_and_used(chrome, tmp_path, monkeypatch):
    other = tmp_path / "other-root"
    (other / "Profile 1").mkdir(parents=True)
    (other / "Profile 1" / "Preferences").write_text("{}")
    bp.bind("alt", "Profile 1", user_data_dir=other)
    monkeypatch.delenv(bp.USER_DATA_ENV)
    assert bp.resolve_binding("alt").profile_path == other / "Profile 1"
    with pytest.raises(bp.BrowserProfileError, match="absolute"):
        bp.bind("rel", "Profile 1", user_data_dir=Path("relative"))


def test_label_selection_rejects_missing_and_ambiguous_matches(chrome):
    bp.bind("a", "Default", sites=["mail.example.test"], workflows=["inbox"])
    bp.bind("b", "Profile 3", sites=["mail.example.test"], workflows=["billing"])
    assert bp.resolve_binding(site="mail.example.test", workflow="billing").alias == "b"
    assert bp.resolve_binding(workflow="inbox").alias == "a"
    with pytest.raises(bp.BrowserProfileError, match="ambiguous between bindings: a, b"):
        bp.resolve_binding(site="mail.example.test")
    with pytest.raises(bp.BrowserProfileError, match="no browser binding is labeled"):
        bp.resolve_binding(site="other.example.test")
    with pytest.raises(bp.BrowserProfileError, match="not both"):
        bp.resolve_binding("a", site="mail.example.test")


@pytest.mark.parametrize("payload", [
    {"version": 1, "bindings": {}, "extra": True},
    {"version": 2, "bindings": {}},
    {"version": 1, "bindings": {"a": {"profile_dir": "Default", "cookie": "x"}}},
    {"version": 1, "bindings": {"a": {"profile_dir": "Default", "mode": "always"}}},
    {"version": 1, "bindings": {"a": {"profile_dir": "Default", "user_data_dir": "rel"}}},
])
def test_sidecar_with_unknown_or_invalid_content_is_refused_not_rewritten(chrome, payload):
    path = bp.sidecar_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    with pytest.raises(bp.BrowserProfileError):
        bp.load_bindings()
    with pytest.raises(bp.BrowserProfileError):
        bp.bind("new", "Default")
    assert json.loads(path.read_text()) == payload


def test_sidecar_readable_by_others_is_refused(chrome):
    bp.bind("main", "Default")
    bp.sidecar_path().chmod(0o644)
    with pytest.raises(bp.BrowserProfileError, match="0600"):
        bp.load_bindings()


def test_failed_write_keeps_previous_sidecar(chrome, monkeypatch):
    bp.bind("main", "Default")
    before = bp.sidecar_path().read_text()

    def broken_dump(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(bp.json, "dump", broken_dump)
    with pytest.raises(bp.BrowserProfileError, match="could not save"):
        bp.bind("work", "Profile 3")
    assert bp.sidecar_path().read_text() == before
    leftovers = [p for p in bp.sidecar_path().parent.iterdir()
                 if p.name.startswith(".browser-profiles.json.") and not p.name.endswith(".lock")]
    assert leftovers == []


@pytest.fixture
def checker(tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    (plugin / "scripts" / "check-extension-installed.js").write_text("")
    monkeypatch.setenv(bp.PLUGIN_DIR_ENV, str(plugin))
    return plugin


@pytest.mark.parametrize(("report", "status"), [
    ({"installed": True, "enabled": True}, "enabled"),
    ({"installed": True, "enabled": False}, "disabled"),
    ({"installed": False, "enabled": False}, "not_installed"),
    ({"installed": True, "enabled": True, "selectedProfileDirectory": "Default"}, "unavailable"),
    ({}, "unavailable"),
])
def test_extension_readiness_uses_vendor_checker_for_bound_profile(chrome, checker, report,
                                                                   status):
    bp.bind("work", "Profile 3")
    resolved = bp.resolve_binding("work")
    seen = {}

    def runner(argv, **kwargs):
        seen.update(argv=argv, env=kwargs["env"])
        return CompletedProcess(argv, 0, stdout=json.dumps(report), stderr="")

    assert bp.extension_readiness(resolved, runner=runner)["status"] == status
    assert seen["argv"][-3:] == ["--browser", "chrome", "--json"]
    assert seen["env"][bp.PREFERENCES_ENV] == str(chrome / "Profile 3" / "Preferences")


def test_extension_readiness_without_plugin_is_unavailable(chrome, tmp_path, monkeypatch):
    monkeypatch.setenv(bp.PLUGIN_DIR_ENV, str(tmp_path / "missing"))
    bp.bind("work", "Profile 3")
    result = bp.extension_readiness(bp.resolve_binding("work"))
    assert result["status"] == "unavailable"


def test_cli_flow_respects_mode_and_email_opt_in(chrome, checker, monkeypatch):
    runner = CliRunner()
    app = browser_cli.browser_app

    listed = runner.invoke(app, ["profiles", "--json"])
    assert listed.exit_code == 0
    assert "email" not in listed.output
    assert runner.invoke(app, ["profiles", "--include-email"]).exit_code != 0
    with_email = json.loads(runner.invoke(app, ["profiles", "--json", "--include-email"]).output)
    assert with_email[0]["email"] == "main@example.test"

    bound = runner.invoke(app, ["bind", "work", "--profile-dir", "Profile 3",
                                "--site", "app.example.test"])
    assert bound.exit_code == 0, bound.output
    exports = runner.invoke(app, ["resolve", "--site", "app.example.test", "--env"])
    assert exports.exit_code == 0
    assert f"export {bp.PREFERENCES_ENV}=" in exports.output

    monkeypatch.setattr(bp, "extension_readiness", lambda resolved: {"status": "enabled"})
    shown = json.loads(runner.invoke(app, ["resolve", "work", "--json"]).output)
    assert (shown["mode"], shown["extension"]["status"], shown["authentication"]) == (
        "browser", "enabled", "not checked")

    assert runner.invoke(app, ["mode", "work", "computer"]).exit_code == 0
    refused = runner.invoke(app, ["resolve", "work", "--env"])
    assert refused.exit_code == 2
    assert "export" not in refused.output
    assert runner.invoke(app, ["mode", "work", "sometimes"]).exit_code == 1

    rows = json.loads(runner.invoke(app, ["bindings", "--json"]).output)
    assert [(r["alias"], r["mode"], r["state"]) for r in rows] == [("work", "computer", "ok")]
    os.remove(chrome / "Profile 3" / "Preferences")
    rows = json.loads(runner.invoke(app, ["bindings", "--json"]).output)
    assert rows[0]["state"] == "missing profile"
    assert runner.invoke(app, ["resolve", "work"]).exit_code == 1
    assert runner.invoke(app, ["unbind", "work"]).exit_code == 0
    assert bp.load_bindings() == {}
