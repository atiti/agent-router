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
chmod 755 "$PAYLOAD/install.sh" "$PAYLOAD/codex-launcher" "$PAYLOAD/codex-bin" \
    "$PAYLOAD/codex-code-mode-host"
if [ "$PLATFORM" = darwin ] && [ -n "${AGENTROUTE_RELEASE_SIGNING_IDENTITY:-}" ]; then
    codesign --force --options runtime --timestamp \
        --sign "$AGENTROUTE_RELEASE_SIGNING_IDENTITY" "$PAYLOAD/codex-bin"
    codesign --force --options runtime --timestamp \
        --sign "$AGENTROUTE_RELEASE_SIGNING_IDENTITY" "$PAYLOAD/codex-code-mode-host"
    codesign --verify --strict --verbose=2 "$PAYLOAD/codex-bin"
    codesign --verify --strict --verbose=2 "$PAYLOAD/codex-code-mode-host"
fi

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
