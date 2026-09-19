#!/bin/sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUTPUT_DIR=${1:-"$PROJECT_ROOT/dist"}
BUILD_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/agentroute-release.XXXXXX")
cleanup() {
    rm -rf "$BUILD_ROOT"
}
trap cleanup EXIT HUP INT TERM

case "$(uname -s)" in
    Darwin) PLATFORM=darwin ;;
    Linux) PLATFORM=linux ;;
    *) printf 'Unsupported release platform: %s\n' "$(uname -s)" >&2; exit 1 ;;
esac
case "$(uname -m)" in
    arm64|aarch64) ARCH=arm64 ;;
    x86_64|amd64) ARCH=x64 ;;
    *) printf 'Unsupported release architecture: %s\n' "$(uname -m)" >&2; exit 1 ;;
esac

PAYLOAD="$BUILD_ROOT/payload"
BUILD_HOME=${AGENTROUTE_RELEASE_PREBUILT_HOME:-"$BUILD_ROOT/home"}
mkdir -p "$PAYLOAD" "$OUTPUT_DIR"
if [ -z "${AGENTROUTE_RELEASE_PREBUILT_HOME:-}" ]; then
    AGENTROUTE_HOME="$BUILD_HOME" AGENTROUTE_INSTALL_ACTIVATE=0 \
        AGENTROUTE_KEEP_BUILD=0 "$PROJECT_ROOT/scripts/install.sh"
fi
for required in codex-bin codex-code-mode-host; do
    if [ ! -x "$BUILD_HOME/bin/$required" ]; then
        printf 'Release build is missing executable %s\n' "$BUILD_HOME/bin/$required" >&2
        exit 1
    fi
done
uv build --wheel --out-dir "$PAYLOAD" "$PROJECT_ROOT"
rm -f "$PAYLOAD/.gitignore"
cp "$BUILD_HOME/bin/codex-bin" "$PAYLOAD/codex-bin"
cp "$BUILD_HOME/bin/codex-code-mode-host" "$PAYLOAD/codex-code-mode-host"
cp "$BUILD_HOME/build-id" "$PAYLOAD/build-id"
cp "$PROJECT_ROOT/packaging/codex-launcher" "$PAYLOAD/codex-launcher"
cp "$PROJECT_ROOT/packaging/install-release.sh" "$PAYLOAD/install.sh"
cp "$PROJECT_ROOT/src/agentroute/code_mode_smoke.py" "$PAYLOAD/code-mode-smoke.py"
chmod 755 "$PAYLOAD/install.sh" "$PAYLOAD/codex-launcher" "$PAYLOAD/codex-bin" \
    "$PAYLOAD/codex-code-mode-host" "$PAYLOAD/code-mode-smoke.py"
if [ "$PLATFORM" = darwin ]; then
    if [ -z "${AGENTROUTE_RELEASE_SIGNING_IDENTITY:-}" ]; then
        printf 'Refusing to build a public macOS release without a Developer ID identity.\n' >&2
        exit 1
    fi
    codesign --force --deep --options runtime --timestamp \
        --sign "$AGENTROUTE_RELEASE_SIGNING_IDENTITY" "$PAYLOAD/codex-bin"
    codesign --force --deep --options runtime --timestamp \
        --entitlements "$PROJECT_ROOT/assets/desktop/codex-code-mode-host.entitlements.plist" \
        --sign "$AGENTROUTE_RELEASE_SIGNING_IDENTITY" "$PAYLOAD/codex-code-mode-host"
    codesign --verify --strict --verbose=2 "$PAYLOAD/codex-bin"
    codesign --verify --strict --verbose=2 "$PAYLOAD/codex-code-mode-host"
    codesign -dv --verbose=4 "$PAYLOAD/codex-bin" 2>&1 \
        | grep -q 'Authority=Developer ID Application:'
    codesign -dv --verbose=4 "$PAYLOAD/codex-code-mode-host" 2>&1 \
        | grep -q 'Authority=Developer ID Application:'
    if [ "${AGENTROUTE_REQUIRE_NOTARIZATION:-0}" = 1 ]; then
        if [ -z "${APPLE_ID:-}" ] \
            || [ -z "${APPLE_TEAM_ID:-}" ] \
            || [ -z "${APPLE_APP_SPECIFIC_PASSWORD:-}" ]; then
            printf 'APPLE_ID, APPLE_TEAM_ID, and APPLE_APP_SPECIFIC_PASSWORD are required for notarization.\n' >&2
            exit 1
        fi
        NOTARY_PAYLOAD="$BUILD_ROOT/notary-payload"
        NOTARY_ARCHIVE="$BUILD_ROOT/agentroute-notarization.zip"
        mkdir -p "$NOTARY_PAYLOAD"
        cp "$PAYLOAD/codex-bin" "$NOTARY_PAYLOAD/codex-bin"
        cp "$PAYLOAD/codex-code-mode-host" "$NOTARY_PAYLOAD/codex-code-mode-host"
        ditto -c -k --sequesterRsrc --keepParent "$NOTARY_PAYLOAD" "$NOTARY_ARCHIVE"
        xcrun notarytool submit "$NOTARY_ARCHIVE" \
            --apple-id "$APPLE_ID" \
            --team-id "$APPLE_TEAM_ID" \
            --password "$APPLE_APP_SPECIFIC_PASSWORD" \
            --wait
    fi
fi
python3 "$PROJECT_ROOT/src/agentroute/code_mode_smoke.py" "$PAYLOAD/codex-code-mode-host"

VERSION=$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$PROJECT_ROOT/pyproject.toml" | head -1)
printf '%s\n' "$VERSION" >"$PAYLOAD/VERSION"
ARTIFACT="$OUTPUT_DIR/agentroute-$PLATFORM-$ARCH.tar.gz"
tar -C "$PAYLOAD" -czf "$ARTIFACT" .
if command -v sha256sum >/dev/null 2>&1; then
    (cd "$OUTPUT_DIR" && sha256sum "$(basename "$ARTIFACT")") >"$ARTIFACT.sha256"
else
    (cd "$OUTPUT_DIR" && shasum -a 256 "$(basename "$ARTIFACT")") >"$ARTIFACT.sha256"
fi
printf '%s\n' "$ARTIFACT"
