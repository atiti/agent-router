import plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_launcher_uses_embedded_codex_and_external_credentials():
    launcher = (ROOT / "assets/desktop/codex-launcher").read_text(encoding="utf-8")
    packaged_launcher = (
        ROOT / "src/agentroute/desktop_assets/codex-launcher"
    ).read_text(encoding="utf-8")

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
