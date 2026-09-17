#!/bin/sh
set -eu

REPOSITORY=${AGENTROUTE_REPOSITORY:-atiti/agent-router}
DOWNLOAD_ROOT=${AGENTROUTE_DOWNLOAD_ROOT:-"https://github.com/$REPOSITORY/releases/latest/download"}
case "$(uname -s)" in
    Darwin) PLATFORM=darwin ;;
    Linux) PLATFORM=linux ;;
    *) printf 'AgentRoute does not publish binaries for %s.\n' "$(uname -s)" >&2; exit 1 ;;
esac
case "$(uname -m)" in
    arm64|aarch64) ARCH=arm64 ;;
    x86_64|amd64) ARCH=x64 ;;
    *) printf 'AgentRoute does not publish binaries for %s.\n' "$(uname -m)" >&2; exit 1 ;;
esac

ASSET="agentroute-$PLATFORM-$ARCH.tar.gz"
DOWNLOAD_DIR=$(mktemp -d "${TMPDIR:-/tmp}/agentroute-download.XXXXXX")
cleanup() {
    rm -rf "$DOWNLOAD_DIR"
}
trap cleanup EXIT HUP INT TERM

curl --fail --location --proto '=https' --tlsv1.2 \
    "$DOWNLOAD_ROOT/$ASSET" --output "$DOWNLOAD_DIR/$ASSET"
curl --fail --location --proto '=https' --tlsv1.2 \
    "$DOWNLOAD_ROOT/$ASSET.sha256" --output "$DOWNLOAD_DIR/$ASSET.sha256"
if command -v sha256sum >/dev/null 2>&1; then
    (cd "$DOWNLOAD_DIR" && sha256sum --check "$ASSET.sha256")
else
    (cd "$DOWNLOAD_DIR" && shasum -a 256 --check "$ASSET.sha256")
fi
mkdir -p "$DOWNLOAD_DIR/payload"
tar -C "$DOWNLOAD_DIR/payload" -xzf "$DOWNLOAD_DIR/$ASSET"
"$DOWNLOAD_DIR/payload/install.sh"
