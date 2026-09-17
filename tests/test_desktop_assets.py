import plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_launcher_uses_embedded_codex_and_external_credentials():
    launcher = (ROOT / "assets/desktop/codex-launcher").read_text(encoding="utf-8")

    assert 'exec "$ROUTED_RESOURCES/codex-bin"' in launcher
    assert '"$AGENTROUTE_HOME_DIR/backend-credentials.env"' in launcher
    assert "--enable step_model_switching" in launcher
    assert "DEEPSEEK_API_KEY=" not in launcher
    assert "AZURE_OPENAI_API_KEY=" not in launcher


def test_adhoc_desktop_entitlements_exclude_openai_restricted_groups():
    path = ROOT / "assets/desktop/ChatGPT-Routed.entitlements.plist"
    with path.open("rb") as handle:
        entitlements = plistlib.load(handle)

    assert entitlements["com.apple.security.cs.allow-jit"] is True
    assert entitlements["com.apple.security.cs.disable-library-validation"] is True
    assert "com.apple.application-identifier" not in entitlements
    assert "com.apple.developer.team-identifier" not in entitlements
    assert "com.apple.security.application-groups" not in entitlements
    assert "keychain-access-groups" not in entitlements
