import base64
import hashlib
import json
import os
import subprocess
from importlib.metadata import PathDistribution
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentroute import provenance
from agentroute.config import SubscriptionProfileConfig, default_config
from agentroute.doctor import DoctorCheck
from agentroute.install_manifest import build_manifest, main, read_manifest, write_manifest
from agentroute.provenance import (
    MIN_PORTABLE_COMPACTION_REVISION,
    ProcessInfo,
    ProvenanceCheck,
    Signature,
    accounts_check,
    build_fingerprint,
    compaction_check,
    compare_fingerprints,
    computer_use_check,
    format_differences,
    install_integrity_check,
    install_provenance_check,
    list_processes,
    parse_codesign,
    parse_etime,
    redact,
    running_runtimes_check,
    runtime_integrity_check,
    shared_server_check,
)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "Users" / "someone"
    home.mkdir(parents=True)
    monkeypatch.setattr(provenance.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("USER", "someone")
    monkeypatch.setenv("LOGNAME", "someone")
    return home


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def make_dist(site: Path, direct_url: dict | None = None, version: str = "1.2.3"):
    package = site / "agentroute"
    package.mkdir(parents=True)
    files = {"agentroute/__init__.py": b"x = 1\n", "agentroute/doctor.py": b"y = 2\n"}
    for name, data in files.items():
        (site / name).write_bytes(data)
    info = site / f"agentroute-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: agentroute\nVersion: {version}\n")
    if direct_url is not None:
        (info / "direct_url.json").write_text(json.dumps(direct_url))
    record = [f"{name},{_record_hash(data)},{len(data)}" for name, data in files.items()]
    record.append(f"{info.name}/RECORD,,")
    (info / "RECORD").write_text("\n".join(record) + "\n")
    return PathDistribution(info), package


# --- install-provenance -------------------------------------------------------


def test_release_wheel_with_matching_manifest_passes(tmp_path):
    dist, package = make_dist(
        tmp_path / "site",
        {"url": "file:///tmp/x/agentroute-1.2.3-py3-none-any.whl", "archive_info": {}},
    )
    manifest = {"agentroute_version": "1.2.3", "source": {"kind": "release", "ref": "v1.2.3"}}
    check = install_provenance_check(dist, package, manifest)
    assert check.status == "pass"
    assert "local-wheel" in check.detail and "release v1.2.3" in check.detail
    assert check.summary["version_matches_manifest"] is True


def test_editable_install_warns_with_redacted_source(tmp_path, fake_home):
    checkout = fake_home / "src" / "agentroute-pr"
    dist, _ = make_dist(
        tmp_path / "site",
        {"url": f"file://{checkout}", "dir_info": {"editable": True}},
    )
    check = install_provenance_check(dist, checkout, {})
    assert check.status == "warn"
    assert "editable install from ~/src/agentroute-pr" in check.detail
    assert str(fake_home) not in check.detail
    assert "no install manifest" in check.detail


def test_vcs_install_and_version_drift_warn(tmp_path):
    dist, package = make_dist(
        tmp_path / "site",
        {"url": "https://example.invalid/agentroute.git", "vcs_info": {"commit_id": "a" * 40}},
    )
    manifest = {"agentroute_version": "1.0.0", "source": {"kind": "release", "ref": "v1.0.0"}}
    check = install_provenance_check(dist, package, manifest)
    assert check.status == "warn"
    assert "VCS install" in check.detail
    assert "differs from the 1.0.0 recorded at install time" in check.detail


def test_code_imported_from_elsewhere_warns(tmp_path):
    dist, _ = make_dist(tmp_path / "site", None)
    elsewhere = tmp_path / "checkout" / "agentroute"
    elsewhere.mkdir(parents=True)
    check = install_provenance_check(dist, elsewhere, {"agentroute_version": "1.2.3"})
    assert check.status == "warn"
    assert "not the installed package" in check.detail
    assert check.summary["kind"] == "index"


def test_missing_distribution_warns(tmp_path):
    check = install_provenance_check(None, tmp_path, {})
    assert check.status == "warn"
    assert check.summary["kind"] == "unpackaged"


# --- install-integrity --------------------------------------------------------


def test_integrity_passes_then_reports_modified_and_missing_files(tmp_path):
    dist, package = make_dist(tmp_path / "site", None)
    check = install_integrity_check(dist)
    assert check.status == "pass"
    assert check.summary["verified"] == 2

    (package / "doctor.py").write_text("patched = True\n")
    (package / "__init__.py").unlink()
    check = install_integrity_check(dist)
    assert check.status == "warn"
    assert check.summary["modified"] == ["agentroute/doctor.py"]
    assert check.summary["missing"] == ["agentroute/__init__.py"]
    assert str(tmp_path) not in check.detail


def test_integrity_without_distribution_warns():
    assert install_integrity_check(None).status == "warn"


# --- install manifest + runtime-integrity ------------------------------------


def _runtime_home(tmp_path: Path) -> Path:
    home = tmp_path / "ar"
    (home / "bin").mkdir(parents=True)
    for name in ("codex-bin", "codex-code-mode-host", "codex"):
        (home / "bin" / name).write_text(f"{name}\n")
    (home / "build-id").write_text("abc-provider-routing-v73\n")
    return home


def test_manifest_round_trip_and_cli(tmp_path):
    home = _runtime_home(tmp_path)
    arguments = ["--home", str(home), "--source-kind", "source", "--source-ref", "c0ffee"]
    assert main([*arguments, "--dirty"]) == 0
    manifest = read_manifest(home)
    assert manifest["source"] == {"kind": "source", "ref": "c0ffee", "dirty": True}
    assert manifest["build_id"] == "abc-provider-routing-v73"
    assert set(manifest["binaries"]) == {"bin/codex-bin", "bin/codex-code-mode-host", "bin/codex"}
    assert str(tmp_path) not in json.dumps(manifest)


def test_runtime_integrity_paths(tmp_path):
    home = _runtime_home(tmp_path)
    missing = runtime_integrity_check(home, None, "abc-provider-routing-v73")
    assert missing.status == "warn" and "provenance unknown" in missing.detail
    assert "agentroute update" in missing.detail
    assert set(missing.summary["binaries"]) == {
        "bin/codex-bin", "bin/codex-code-mode-host", "bin/codex"
    }

    manifest = build_manifest(home, "release", "v1.2.3")
    write_manifest(home, manifest)
    ok = runtime_integrity_check(home, read_manifest(home), "abc-provider-routing-v73")
    assert ok.status == "pass" and "3 runtime files match" in ok.detail

    (home / "bin" / "codex-bin").write_text("hand patched\n")
    changed = runtime_integrity_check(home, manifest, "abc-provider-routing-v73")
    assert changed.status == "warn"
    assert "binary changed since install" in changed.detail
    assert "bin/codex-bin changed" in changed.detail

    other_build = runtime_integrity_check(
        home, build_manifest(home, "release", "v1"), "xyz-provider-routing-v70"
    )
    assert other_build.status == "warn" and "build-id" in other_build.detail


# --- running-runtimes ---------------------------------------------------------


def test_parse_etime():
    assert parse_etime("05") == 5
    assert parse_etime("01:05") == 65
    assert parse_etime("02:01:05") == 7265
    assert parse_etime("1-00:00:01") == 86401
    assert parse_etime("bogus") is None


def test_running_runtimes_reports_stale_private_and_services(tmp_path, fake_home):
    home = _runtime_home(tmp_path)
    installed = str((home / "bin" / "codex-bin").resolve())
    desktop = tmp_path / "Routed.app"
    now = 10_000.0
    processes = [
        ProcessInfo(990101, installed, "codex", 100.0),  # started after install
        ProcessInfo(990102, installed, "codex", 5_000.0),  # started before install
        ProcessInfo(990103, str(fake_home / "build/codex/target/dev/codex"), "codex", 1.0),
        ProcessInfo(990104, "/opt/private/bin/codex-bin", "secret prompt text", 1.0),
        ProcessInfo(990105, str(desktop / "Contents/Resources/codex-bin"), "", 1.0),
        ProcessInfo(990106, "/Applications/Vendor.app/Contents/Resources/codex", "", 1.0),
        ProcessInfo(990107, "/usr/bin/python3", "python3 -m agentroute.bridge_server", 9_000.0),
        ProcessInfo(990108, "/usr/bin/python3", "python3 -m mlx_server --dir agentroute", 1.0),
        ProcessInfo(990109, "/bin/zsh", "zsh -c agentroute doctor", 1.0),
    ]
    check = running_runtimes_check(
        home, 8_000.0, processes, desktop, which=lambda _: str(home / "bin" / "codex"), now=now
    )
    assert check.status == "warn"
    summary = check.summary
    assert summary["installed"] == 2 and summary["installed_stale"] == 1
    assert summary["routed_desktop"] == 1
    assert summary["services"] == 1 and summary["services_stale"] == 1
    assert set(summary["unexpected_runtimes"]) == {
        "~/build/codex/target/dev/codex", "/opt/private/bin/codex-bin"
    }
    assert "/Applications/Vendor.app/Contents/Resources/codex" in summary["other_runtimes"]
    assert "1 process still on the previous build" in check.detail
    assert "secret prompt text" not in json.dumps(summary) + check.detail
    assert str(fake_home) not in check.detail


def test_running_runtimes_clean_and_path_mismatch(tmp_path):
    home = _runtime_home(tmp_path)
    installed = str((home / "bin" / "codex-bin").resolve())
    clean = running_runtimes_check(
        home, 0.0, [ProcessInfo(5, installed, "", 1.0)], None,
        which=lambda _: str(home / "bin" / "codex"), now=100.0,
    )
    assert clean.status == "pass"
    assert "codex on PATH is the AgentRoute launcher" in clean.detail
    elsewhere = running_runtimes_check(home, 0.0, [], None, which=lambda _: "/usr/bin/codex")
    assert elsewhere.status == "warn" and "not the AgentRoute launcher" in elsewhere.detail


def test_running_runtimes_without_process_listing(tmp_path):
    home = _runtime_home(tmp_path)
    check = running_runtimes_check(home, None, None, None, which=lambda _: None)
    assert check.status == "pass" and "unavailable" in check.detail


def test_list_processes_degrades_gracefully(monkeypatch):
    assert list_processes("Windows") is None
    monkeypatch.setattr(provenance, "_run", lambda *a, **k: None)
    assert list_processes("Darwin") is None


def test_list_processes_parses_macos_paths_with_spaces(monkeypatch):
    outputs = {
        "pid=,etime=,comm=": " 10  01:00 /Applications/Some App.app/Contents/MacOS/codex\n",
        "pid=,args=": " 10 codex --flag\n",
    }

    def fake_run(command, timeout=3.0):
        return subprocess.CompletedProcess(command, 0, outputs[command[2]], "")

    monkeypatch.setattr(provenance, "_run", fake_run)
    [process] = list_processes("Darwin")
    assert process.exe == "/Applications/Some App.app/Contents/MacOS/codex"
    assert process.age_seconds == 60 and process.args == "codex --flag"


# --- shared-server ------------------------------------------------------------


def test_shared_server_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr("agentroute.shared_server._metadata", lambda: {})
    check = shared_server_check(default_config(), tmp_path)
    assert check.status == "pass" and check.summary["running"] is False


def test_shared_server_fingerprint_match_and_mismatch(tmp_path, monkeypatch):
    home = _runtime_home(tmp_path)
    binary = str((home / "bin" / "codex-bin").resolve())
    metadata = {"pid": os.getpid(), "binary": binary, "fingerprint": "current"}
    monkeypatch.setattr("agentroute.shared_server._metadata", lambda: metadata)
    monkeypatch.setattr(
        "agentroute.shared_server.server_environment", lambda prune=True: {"prune": str(prune)}
    )
    seen = {}

    def fake_fingerprint(path, config, environment):
        seen["environment"] = environment
        return "current"

    monkeypatch.setattr("agentroute.shared_server._fingerprint", fake_fingerprint)
    check = shared_server_check(default_config(), home)
    assert check.status == "pass" and check.summary["fingerprint_match"] is True
    assert seen["environment"] == {"prune": "False"}  # never prunes diagnostics

    metadata["fingerprint"] = "older"
    check = shared_server_check(default_config(), home)
    assert check.status == "warn"
    assert "agentroute server stop" in check.detail
    assert check.detail.index("Close attached clients") < check.detail.index("server stop")
    assert "older" not in json.dumps(check.summary)


def test_shared_server_fingerprint_error_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agentroute.shared_server._metadata", lambda: {"pid": os.getpid(), "fingerprint": "x"}
    )

    def broken(prune=True):
        raise RuntimeError("bad credential file")

    monkeypatch.setattr("agentroute.shared_server.server_environment", broken)
    check = shared_server_check(default_config(), tmp_path)
    assert check.status == "warn" and "RuntimeError" in check.detail


# --- computer-use -------------------------------------------------------------


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def _cu_settings(command: Path, service_app: Path | None = None) -> dict:
    env = {"NODE_REPL_NODE_PATH": str(command)}
    if service_app is not None:
        env["SOME_CUA_SERVICE_PATH"] = str(service_app)
    return {"mcp_servers": {
        "node_repl": {"command": str(command), "env": env},
        "docs": {"command": "/usr/bin/true"},
    }}


def test_computer_use_not_configured():
    check = computer_use_check({"mcp_servers": {"docs": {"command": "x"}}}, Path("/"), "Darwin")
    assert check.status == "pass" and check.detail == "not configured"
    disabled = computer_use_check(
        {"mcp_servers": {"computer-use": {"command": "./c", "enabled": False}}}, Path("/"), "Darwin"
    )
    assert disabled.status == "pass" and "1 disabled server" in disabled.detail


def test_computer_use_skips_signatures_on_linux(tmp_path):
    tool = _executable(tmp_path / "Vendor.app/Contents/Resources/node_repl")

    def never(path):
        raise AssertionError("codesign must not run on Linux")

    check = computer_use_check(_cu_settings(tool), tmp_path, "Linux", never)
    assert check.status == "pass"
    assert "signature checks skipped on linux" in check.detail


def test_computer_use_team_id_must_match_enclosing_app(tmp_path):
    app = tmp_path / "Vendor.app"
    tool = _executable(app / "Contents/Resources/node_repl")
    teams = {app: Signature("developer-id", "TEAMA"), tool: Signature("developer-id", "TEAMA")}
    check = computer_use_check(_cu_settings(tool), tmp_path, "Darwin", teams.get)
    assert check.status == "pass"
    assert check.summary["paths"][0]["team_matches_app"] is True

    teams[tool] = Signature("developer-id", "TEAMB")
    check = computer_use_check(_cu_settings(tool), tmp_path, "Darwin", teams.get)
    assert check.status == "warn" and "Team ID of its enclosing app" in check.detail
    assert "TEAMA" not in check.detail and "TEAMB" not in check.detail


def test_computer_use_flags_adhoc_copies_missing_paths_and_routed_app(tmp_path, fake_home):
    copy = fake_home / "copies/Service.app"
    copy.mkdir(parents=True)
    tool = _executable(fake_home / "bin/node_repl")
    signatures = {copy: Signature("adhoc", None), tool: Signature("unsigned", None)}
    check = computer_use_check(_cu_settings(tool, copy), tmp_path, "Darwin", signatures.get)
    assert check.status == "warn"
    assert "adhoc signature on ~/copies/Service.app" in check.detail
    assert "unsigned signature on ~/bin/node_repl" in check.detail
    assert str(fake_home) not in check.detail

    gone = computer_use_check(_cu_settings(tmp_path / "missing/node_repl"), tmp_path, "Darwin",
                              signatures.get)
    assert gone.status == "warn" and "missing" in gone.detail

    routed = tmp_path / "Routed.app"
    inside = _executable(routed / "Contents/Resources/node_repl")
    check = computer_use_check(
        _cu_settings(inside), tmp_path, "Darwin",
        lambda path: Signature("adhoc", None), routed_desktop=routed,
    )
    assert "AgentRoute-rebuilt Desktop app" in check.detail


def test_computer_use_without_codesign_tool(tmp_path):
    tool = _executable(tmp_path / "node_repl")
    check = computer_use_check(_cu_settings(tool), tmp_path, "Darwin", lambda path: None)
    assert check.status == "pass" and "codesign unavailable" in check.detail


def test_computer_use_resolves_relative_commands_and_trusted_services(tmp_path):
    app = tmp_path / "cu" / "Tool.app"
    _executable(app / "Contents/MacOS/Tool")
    service = tmp_path / "service.mjs"
    service.write_text("export {}\n")
    settings = {
        "mcp_servers": {"computer-use": {
            "command": "./Tool.app/Contents/MacOS/Tool", "cwd": str(tmp_path / "cu"),
            "env": {"X_TRUSTED_SERVICES": json.dumps({"browser": str(service), "pkg": "@scope/x"})},
        }},
    }
    check = computer_use_check(settings, tmp_path, "Linux")
    refs = {entry["ref"] for entry in check.summary["paths"]}
    assert refs == {"computer-use.command", "computer-use.X_TRUSTED_SERVICES.browser"}
    assert check.status == "pass"


def test_parse_codesign_variants():
    assert parse_codesign("x: code object is not signed at all", 1).kind == "unsigned"
    adhoc = parse_codesign("Signature=adhoc\nTeamIdentifier=not set\n", 0)
    assert adhoc == Signature("adhoc", None)
    signed = parse_codesign(
        "Authority=Developer ID Application: Someone (ABC)\nTeamIdentifier=ABC\n", 0
    )
    assert signed == Signature("developer-id", "ABC")


# --- accounts -----------------------------------------------------------------


def _profile_home(tmp_path: Path, name: str, account: str | None, refreshed: str | None = None):
    home = tmp_path / name
    home.mkdir()
    payload: dict = {}
    if account is not None:
        payload["tokens"] = {"account_id": account}
    if refreshed:
        payload["last_refresh"] = refreshed
    (home / "auth.json").write_text(json.dumps(payload))
    return SubscriptionProfileConfig(credential_home=str(home))


def test_accounts_detects_duplicates_missing_and_stale(tmp_path):
    config = default_config()
    config.capacity.profiles = {}
    assert accounts_check(config).status == "pass"
    config.capacity.profiles = {
        "work": _profile_home(tmp_path, "work", "acct-secret-1", "2026-11-27T00:00:00Z"),
        "person@example.com": _profile_home(tmp_path, "dup", "acct-secret-1"),
        "empty": _profile_home(tmp_path, "empty", None),
        "old": _profile_home(tmp_path, "old", "acct-2", "2026-01-01T00:00:00Z"),
    }
    now = 1_795_000_000.0  # 2026-11-18
    check = accounts_check(config, now=now)
    assert check.status == "warn"
    assert "same account signed in under several profiles" in check.detail
    assert "no stored login: empty" in check.detail
    assert "old (" in check.detail
    assert "acct-secret-1" not in check.detail and "person@example.com" not in check.detail
    assert check.summary == {"profiles": 4, "duplicates": True, "missing": 1, "stale": 1}


# --- compaction ---------------------------------------------------------------


def test_compaction_revision_gate():
    ok = compaction_check(f"abc-provider-routing-v{MIN_PORTABLE_COMPACTION_REVISION}")
    assert ok.status == "pass"
    old = compaction_check("abc-provider-routing-v61")
    assert old.status == "warn" and "agentroute update" in old.detail
    assert compaction_check("missing").status == "warn"


# --- fingerprint, redaction and compare ---------------------------------------


def test_fingerprint_redacts_secrets_paths_emails_and_ids(tmp_path, fake_home, monkeypatch):
    secret = "sk-live-SECRETVALUE123"
    monkeypatch.setenv("AZURE_TEST_KEY", secret)
    config = default_config()
    backend = next(iter(config.backends.values()))
    backend.base_url = "https://user:hunter2@example.invalid/v1"
    backend.api_key_env = "AZURE_TEST_KEY"
    config.capacity.profiles = {
        "someone@example.com": SubscriptionProfileConfig(credential_home=str(fake_home / "acct")),
    }
    provenance_rows = [
        ProvenanceCheck("install-provenance", "warn", "", {
            "kind": "editable", "source": f"{fake_home}/src/agentroute",
            "owner": "someone@example.com",
        }),
        ProvenanceCheck("install-integrity", "warn", "", {
            "verified": 2, "modified": ["agentroute/doctor.py"], "missing": [],
        }),
        ProvenanceCheck("running-runtimes", "warn", "", {
            "other_runtimes": {f"{fake_home}/bin/codex-bin": 1,
                               "/home/otheruser/bin/codex": 1},
        }),
        ProvenanceCheck("shared-server", "pass", "", {
            "running": True, "fingerprint_match": False, "fingerprint": "deadbeef" * 8,
        }),
        ProvenanceCheck("accounts", "pass", "", {"profiles": 1, "duplicates": False}),
        ProvenanceCheck("computer-use", "pass", "", {
            "paths": [{"path": f"{fake_home}/.codex/x.app", "signature": "adhoc"}],
        }),
    ]
    checks = [DoctorCheck("config", "pass", f"loaded {fake_home}/.agentroute/config.yaml")]
    snapshot = build_fingerprint(config, checks, provenance_rows, "abc-provider-routing-v73")
    text = json.dumps(snapshot)
    for leaked in (secret, "hunter2", "example.invalid", "someone@example.com", str(fake_home),
                   "otheruser", "someone", "deadbeef", "acct"):
        assert leaked not in text, leaked
    assert snapshot["config"]["profile_count"] == 1
    assert snapshot["shared_server"] == {"running": True, "fingerprint_match": False}
    assert snapshot["checks"] == {"config": "pass"}
    assert snapshot["runtime"]["revision"] == 73
    assert snapshot["integrity"]["modified"] == ["agentroute/doctor.py"]


def test_redact_handles_home_users_urls_and_hostnames(fake_home, monkeypatch):
    monkeypatch.setattr(provenance.socket, "gethostname", lambda: "workstation-42.local")
    text = redact(
        f"{fake_home}/x /Users/bob/y https://a:b@host/z mail me@example.org on workstation-42"
        " /var/folders/ab/cd/T/agentroute-update-1/x.whl /tmp/x-someone-y"
    )
    assert text == (
        "~/x /<home>/y https://<redacted>@host/z mail <email> on <host>"
        " $TMPDIR/agentroute-update-1/x.whl /tmp/x-<user>-y"
    )


def test_compare_groups_differences():
    theirs = {"schema": 1, "runtime": {"build_id": "a-v72"},
              "integrity": {"modified": ["agentroute/doctor.py"]}, "checks": {"x": "pass"}}
    ours = {"schema": 1, "runtime": {"build_id": "a-v73"},
            "integrity": {"modified": []}, "checks": {"x": "pass"}}
    differences = compare_fingerprints(theirs, ours)
    assert [(item["section"], item["key"]) for item in differences] == [
        ("runtime", "build_id"), ("integrity", "modified")
    ]
    text = format_differences(differences, (1, 1))
    assert "runtime:\n  build_id: a-v72 (theirs) vs a-v73 (ours)" in text
    assert "modified: agentroute/doctor.py (theirs) vs (none) (ours)" in text
    assert format_differences([], (1, 2)).splitlines() == [
        "fingerprint schema: 1 vs 2", "No differences."
    ]


def test_cli_fingerprint_and_compare(tmp_path, monkeypatch):
    from agentroute import cli

    rows = [ProvenanceCheck("compaction", "pass", "ok", {"revision": 73})]
    checks = (DoctorCheck("compaction", "pass", "ok"),)
    monkeypatch.setattr(
        cli, "run_doctor_report", lambda config: (checks, rows, "b-provider-routing-v73")
    )
    runner = CliRunner()
    result = runner.invoke(cli.app, ["doctor", "--fingerprint"])
    assert result.exit_code == 0, result.output
    snapshot = json.loads(result.output)
    assert snapshot["runtime"]["build_id"] == "b-provider-routing-v73"

    snapshot["runtime"]["build_id"] = "b-provider-routing-v70"
    snapshot["checks"]["compaction"] = "warn"
    path = tmp_path / "theirs.json"
    path.write_text(json.dumps(snapshot))
    result = runner.invoke(cli.app, ["doctor", "--compare", str(path)])
    assert result.exit_code == 0, result.output
    expected = "build_id: b-provider-routing-v70 (theirs) vs b-provider-routing-v73 (ours)"
    assert expected in result.output
    assert "compaction: warn (theirs) vs pass (ours)" in result.output

    result = runner.invoke(cli.app, ["doctor", "--compare", str(path), "--json"])
    payload = json.loads(result.output)
    assert {item["section"] for item in payload["differences"]} == {"runtime", "checks"}


def test_run_doctor_adds_provenance_rows_that_never_fail(monkeypatch):
    from agentroute.doctor import run_doctor

    monkeypatch.setattr(provenance, "list_processes", lambda system=None: [])
    checks = {check.name: check for check in run_doctor(default_config())}
    for name in ("install-provenance", "install-integrity", "runtime-integrity",
                 "running-runtimes", "shared-server", "computer-use", "accounts", "compaction"):
        assert checks[name].status in {"pass", "warn"}, name


def test_shared_server_fingerprint_ignores_rebuilt_binary_with_same_build_id(tmp_path):
    import os
    from agentroute import shared_server
    from agentroute.config import default_config

    binary = tmp_path / "codex-bin"
    binary.write_bytes(b"one")
    config = default_config()
    env = {"AGENTROUTE_RUNTIME_BUILD_ID": "abc123-provider-routing-v73"}
    first = shared_server._fingerprint(binary, config, env)
    binary.write_bytes(b"rebuilt binary with a different size")
    os.utime(binary, (1, 1))
    assert shared_server._fingerprint(binary, config, env) == first
    assert shared_server._fingerprint(
        binary, config, {"AGENTROUTE_RUNTIME_BUILD_ID": "def456-provider-routing-v74"}
    ) != first


def test_shared_server_fingerprint_falls_back_to_binary_identity_without_build_id(tmp_path):
    from agentroute import shared_server
    from agentroute.config import default_config

    binary = tmp_path / "codex-bin"
    binary.write_bytes(b"one")
    config = default_config()
    first = shared_server._fingerprint(binary, config, {})
    binary.write_bytes(b"different size")
    assert shared_server._fingerprint(binary, config, {}) != first
