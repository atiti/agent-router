#!/bin/sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SOURCE_APP=${1:-/Applications/ChatGPT.app}
DESTINATION_APP=${2:-/Applications/ChatGPT-Routed.app}
SIGNING_IDENTITY=${AGENTROUTE_DESKTOP_SIGNING_IDENTITY:--}
AGENTROUTE_HOME_DIR=${AGENTROUTE_HOME:-"$HOME/.agentroute"}
ROUTED_CODEX="$AGENTROUTE_HOME_DIR/bin/codex-bin"
ROUTED_CODE_MODE_HOST="$AGENTROUTE_HOME_DIR/bin/codex-code-mode-host"
LAUNCHER="$PROJECT_ROOT/assets/desktop/codex-launcher"
ADHOC_ENTITLEMENTS="$PROJECT_ROOT/assets/desktop/ChatGPT-Routed.entitlements.plist"

if [ ! -d "$SOURCE_APP" ]; then
    printf 'Source app does not exist: %s\n' "$SOURCE_APP" >&2
    exit 1
fi
if [ -e "$DESTINATION_APP" ]; then
    printf 'Destination already exists; refusing to overwrite it: %s\n' "$DESTINATION_APP" >&2
    exit 1
fi
for required in "$ROUTED_CODEX" "$ROUTED_CODE_MODE_HOST" "$LAUNCHER"; do
    if [ ! -x "$required" ]; then
        printf 'Required executable does not exist: %s\n' "$required" >&2
        exit 1
    fi
done
SOURCE_CODEX="$SOURCE_APP/Contents/Resources/codex"
SOURCE_VERSION=$("$SOURCE_CODEX" --version 2>/dev/null || true)
ROUTED_VERSION=$("$ROUTED_CODEX" --version 2>/dev/null || true)
if [ "${AGENTROUTE_ALLOW_DESKTOP_VERSION_MISMATCH:-0}" != 1 ] \
    && [ "$SOURCE_VERSION" != "$ROUTED_VERSION" ]; then
    printf 'Official and routed Codex versions differ (%s != %s).\n' \
        "${SOURCE_VERSION:-unknown}" "${ROUTED_VERSION:-unknown}" >&2
    printf 'Update AgentRoute first; a mismatch can break mobile remote connections.\n' >&2
    exit 1
fi

DESTINATION_PARENT=$(dirname -- "$DESTINATION_APP")
DESTINATION_NAME=$(basename -- "$DESTINATION_APP")
mkdir -p "$DESTINATION_PARENT"
STAGING_ROOT=$(mktemp -d "$DESTINATION_PARENT/.ChatGPT-Routed.build.XXXXXX")
STAGING_APP="$STAGING_ROOT/$DESTINATION_NAME"
cleanup() {
    if [ -d "$STAGING_ROOT" ]; then
        rm -rf "$STAGING_ROOT"
    fi
}
trap cleanup EXIT HUP INT TERM

printf 'Copying %s...\n' "$SOURCE_APP"
ditto "$SOURCE_APP" "$STAGING_APP"

RESOURCES="$STAGING_APP/Contents/Resources"
INFO_PLIST="$STAGING_APP/Contents/Info.plist"
cp "$ROUTED_CODEX" "$RESOURCES/codex-bin"
cp "$ROUTED_CODE_MODE_HOST" "$RESOURCES/codex-code-mode-host"
cp "$LAUNCHER" "$RESOURCES/codex"
chmod 755 "$RESOURCES/codex" "$RESOURCES/codex-bin" "$RESOURCES/codex-code-mode-host"

plutil -replace CFBundleDisplayName -string 'ChatGPT-Routed' "$INFO_PLIST"
plutil -replace CFBundleName -string 'ChatGPT-Routed' "$INFO_PLIST"
plutil -replace LSHasLocalizedDisplayName -bool false "$INFO_PLIST"
plutil -replace SUEnableAutomaticChecks -bool false "$INFO_PLIST"
plutil -replace SUAutomaticallyUpdate -bool false "$INFO_PLIST"
plutil -insert AgentRouteDesktopBuild -string "$(sed -n '1p' "$AGENTROUTE_HOME_DIR/build-id")" "$INFO_PLIST"

codesign --force --sign "$SIGNING_IDENTITY" "$RESOURCES/codex-bin"
codesign --force --sign "$SIGNING_IDENTITY" "$RESOURCES/codex-code-mode-host"

codesign --force --deep --sign "$SIGNING_IDENTITY" --options runtime \
    --entitlements "$ADHOC_ENTITLEMENTS" "$STAGING_APP"

codesign --verify --deep --strict --verbose=4 "$STAGING_APP"
"$RESOURCES/codex" app-server --help >/dev/null
mv "$STAGING_APP" "$DESTINATION_APP"

printf 'Created %s\n' "$DESTINATION_APP"
printf 'Signing identity: %s\n' "$SIGNING_IDENTITY"
printf 'Bundle identifier retained for compatibility: %s\n' \
    "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$DESTINATION_APP/Contents/Info.plist")"
