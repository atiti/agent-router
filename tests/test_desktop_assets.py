import plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_launcher_uses_embedded_codex_and_external_credentials():
    launcher = (ROOT / "assets/desktop/codex-launcher").read_text(encoding="utf-8")
    packaged_launcher = (ROOT / "src/agentroute/desktop_assets/codex-launcher").read_text(
        encoding="utf-8"
    )

    assert launcher == packaged_launcher
    assert 'exec "$AGENTROUTE_HOME_DIR/bin/agentroute" launch-codex' in launcher
    assert '--binary "$ROUTED_RESOURCES/codex-bin"' in launcher
    assert '"$AGENTROUTE_HOME_DIR/backend-credentials.env"' in launcher
    routed_launcher = (ROOT / "src/agentroute/launcher.py").read_text(encoding="utf-8")
    assert '"step_model_switching"' in routed_launcher
    assert "DEEPSEEK_API_KEY=" not in launcher
    assert "AZURE_OPENAI_API_KEY=" not in launcher


def test_adhoc_desktop_entitlements_exclude_openai_restricted_groups():
    path = ROOT / "assets/desktop/ChatGPT-Routed.entitlements.plist"
    packaged_path = ROOT / "src/agentroute/desktop_assets/ChatGPT-Routed.entitlements.plist"
    assert path.read_bytes() == packaged_path.read_bytes()
    with path.open("rb") as handle:
        entitlements = plistlib.load(handle)

    assert entitlements["com.apple.security.cs.allow-jit"] is True
    assert entitlements["com.apple.security.cs.disable-library-validation"] is True
    assert "com.apple.application-identifier" not in entitlements
    assert "com.apple.developer.team-identifier" not in entitlements
    assert "com.apple.security.application-groups" not in entitlements
    assert "keychain-access-groups" not in entitlements


def test_code_mode_host_entitlements_are_minimal_and_packaged():
    path = ROOT / "assets/desktop/codex-code-mode-host.entitlements.plist"
    packaged_path = ROOT / "src/agentroute/desktop_assets/codex-code-mode-host.entitlements.plist"
    assert path.read_bytes() == packaged_path.read_bytes()
    with path.open("rb") as handle:
        entitlements = plistlib.load(handle)

    assert entitlements == {"com.apple.security.cs.allow-jit": True}


def test_nested_launcher_uses_embedded_build_and_binary(tmp_path):
    import os
    import subprocess

    resources = tmp_path / "App.app/Contents/Resources"
    entry = resources / "codex-cli/bin/codex"
    entry.parent.mkdir(parents=True)
    entry.write_bytes((ROOT / "assets/desktop/codex-launcher").read_bytes())
    entry.chmod(0o755)
    (resources / "codex-bin").write_text("#!/bin/sh\nexit 0\n")
    (resources / "codex-bin").chmod(0o755)
    (resources / "agentroute-build-id").write_text("embedded-v47\n")
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    (home / "build-id").write_text("different-cli-build\n")
    route = home / "bin/agentroute"
    route.write_text(
        '#!/bin/sh\n[ "$1" = classifier-refresh ] && exit 0\n'
        'printf "%s\\n" "$AGENTROUTE_RUNTIME_BUILD_ID" "$@"\n'
    )
    route.chmod(0o755)
    result = subprocess.run(
        [str(entry), "app-server", "--help"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "AGENTROUTE_HOME": str(home)},
    )
    assert result.stdout.splitlines() == [
        "embedded-v47",
        "launch-codex",
        "--binary",
        str(resources / "codex-bin"),
        "--",
        "app-server",
        "--help",
    ]
