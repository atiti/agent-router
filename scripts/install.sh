#!/bin/sh
set -eu

AGENTROUTE_PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
AGENTROUTE_HOME_DIR=${AGENTROUTE_HOME:-"$HOME/.agentroute"}
AGENTROUTE_BIN_DIR="$AGENTROUTE_HOME_DIR/bin"
AGENTROUTE_CODEX_SOURCE="$AGENTROUTE_HOME_DIR/src/codex-stack"
AGENTROUTE_CODEX_TARGET=${AGENTROUTE_CODEX_TARGET:-"$AGENTROUTE_HOME_DIR/build/codex"}
AGENTROUTE_BUILD_PROFILE=${AGENTROUTE_BUILD_PROFILE:-dev-small}
AGENTROUTE_CODEX_COMMIT=90f76f2013f028b3f9bd3a587bf151cb4934bcb4
AGENTROUTE_CODEX_UPSTREAM=00c972ed5d6ff6499317fd41b7f23605b8e6850d
AGENTROUTE_CODEX_REPOSITORY=https://github.com/atiti/codex.git
AGENTROUTE_CODE_MODE_HOST_VERSION=${AGENTROUTE_CODE_MODE_HOST_VERSION:-0.157.0}
AGENTROUTE_BUILD_ID="$AGENTROUTE_CODEX_COMMIT-provider-routing-v41"
AGENTROUTE_BUILD_ID_FILE="$AGENTROUTE_HOME_DIR/build-id"

for command_name in git cargo uv npm; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        printf 'Missing required command: %s\n' "$command_name" >&2
        exit 1
    fi
done

mkdir -p "$AGENTROUTE_BIN_DIR" "$AGENTROUTE_HOME_DIR/src" "$AGENTROUTE_HOME_DIR/build"

AGENTROUTE_INSTALLED_BUILD_ID=
if [ -f "$AGENTROUTE_BUILD_ID_FILE" ]; then
    AGENTROUTE_INSTALLED_BUILD_ID=$(sed -n '1p' "$AGENTROUTE_BUILD_ID_FILE")
fi
if [ ! -x "$AGENTROUTE_BIN_DIR/codex-bin" ] \
    || [ ! -x "$AGENTROUTE_BIN_DIR/codex-code-mode-host" ] \
    || [ "$AGENTROUTE_INSTALLED_BUILD_ID" != "$AGENTROUTE_BUILD_ID" ]; then
    AGENTROUTE_DEFAULT_MIN_FREE_KB=6291456
    if [ -d "$AGENTROUTE_CODEX_TARGET/$AGENTROUTE_BUILD_PROFILE" ]; then
        AGENTROUTE_DEFAULT_MIN_FREE_KB=2097152
    fi
    AGENTROUTE_MIN_FREE_KB=${AGENTROUTE_MIN_FREE_KB:-$AGENTROUTE_DEFAULT_MIN_FREE_KB}
    AGENTROUTE_FREE_KB=$(df -Pk "$AGENTROUTE_HOME_DIR" | awk 'NR == 2 { print $4 }')
    if [ "$AGENTROUTE_FREE_KB" -lt "$AGENTROUTE_MIN_FREE_KB" ]; then
        printf 'AgentRoute needs at least %s MiB free for this build; only %s MiB is available.\n' \
            "$((AGENTROUTE_MIN_FREE_KB / 1024))" "$((AGENTROUTE_FREE_KB / 1024))" >&2
        printf 'Free disk space and rerun this installer. Existing partial state is reusable.\n' >&2
        exit 1
    fi
fi

AGENTROUTE_STOCK_CODEX=$(command -v codex 2>/dev/null || true)
if [ -L "$AGENTROUTE_BIN_DIR/codex-stock" ]; then
    AGENTROUTE_STOCK_CODEX=$(readlink "$AGENTROUTE_BIN_DIR/codex-stock")
fi

if [ ! -d "$AGENTROUTE_CODEX_SOURCE/.git" ]; then
    git clone --filter=blob:none --no-checkout "$AGENTROUTE_CODEX_REPOSITORY" "$AGENTROUTE_CODEX_SOURCE"
fi

# A pristine, immutable fork revision already contains the reviewed commit stack.
# Refuse dirty build sources; preserve developer edits instead of resetting/stashing them.
if [ -n "$(git -C "$AGENTROUTE_CODEX_SOURCE" status --porcelain)" ]; then
    printf 'Codex stack source is dirty; preserve/commit edits before building: %s\n' \
        "$AGENTROUTE_CODEX_SOURCE" >&2
    exit 1
fi
git -C "$AGENTROUTE_CODEX_SOURCE" fetch "$AGENTROUTE_CODEX_REPOSITORY" "$AGENTROUTE_CODEX_COMMIT"
git -C "$AGENTROUTE_CODEX_SOURCE" merge-base --is-ancestor \
    "$AGENTROUTE_CODEX_UPSTREAM" "$AGENTROUTE_CODEX_COMMIT"
git -C "$AGENTROUTE_CODEX_SOURCE" checkout --detach "$AGENTROUTE_CODEX_COMMIT"
# End immutable source preparation.

if [ ! -x "$AGENTROUTE_HOME_DIR/venv/bin/python" ]; then
    uv venv --python 3.12 "$AGENTROUTE_HOME_DIR/venv"
fi
uv pip install --python "$AGENTROUTE_HOME_DIR/venv/bin/python" "$AGENTROUTE_PROJECT_ROOT"
ln -sf "$AGENTROUTE_HOME_DIR/venv/bin/agentroute" "$AGENTROUTE_BIN_DIR/agentroute"

if [ ! -x "$AGENTROUTE_BIN_DIR/codex-bin" ] \
    || [ ! -x "$AGENTROUTE_BIN_DIR/codex-code-mode-host" ] \
    || [ "$AGENTROUTE_INSTALLED_BUILD_ID" != "$AGENTROUTE_BUILD_ID" ]; then
    AGENTROUTE_SOURCE_CODE_MODE_HOST=${AGENTROUTE_CODE_MODE_HOST:-}
    AGENTROUTE_CODE_MODE_HOST_TMP=
    if [ -z "$AGENTROUTE_SOURCE_CODE_MODE_HOST" ]; then
        case "$(uname -s)" in
            Darwin) AGENTROUTE_NPM_PLATFORM=darwin ;;
            Linux) AGENTROUTE_NPM_PLATFORM=linux ;;
            *)
                printf 'AgentRoute cannot install Code Mode on unsupported platform %s.\n' "$(uname -s)" >&2
                exit 1
                ;;
        esac
        case "$(uname -m)" in
            arm64|aarch64) AGENTROUTE_NPM_ARCH=arm64 ;;
            x86_64|amd64) AGENTROUTE_NPM_ARCH=x64 ;;
            *)
                printf 'AgentRoute cannot install Code Mode on unsupported architecture %s.\n' "$(uname -m)" >&2
                exit 1
                ;;
        esac
        AGENTROUTE_CODE_MODE_HOST_TMP=$(mktemp -d "${TMPDIR:-/tmp}/agentroute-code-mode-host.XXXXXX")
        AGENTROUTE_CODE_MODE_HOST_PACKAGE=$(npm pack \
            "@openai/codex@$AGENTROUTE_CODE_MODE_HOST_VERSION-$AGENTROUTE_NPM_PLATFORM-$AGENTROUTE_NPM_ARCH" \
            --pack-destination "$AGENTROUTE_CODE_MODE_HOST_TMP")
        tar -xzf "$AGENTROUTE_CODE_MODE_HOST_TMP/$AGENTROUTE_CODE_MODE_HOST_PACKAGE" \
            -C "$AGENTROUTE_CODE_MODE_HOST_TMP"
        AGENTROUTE_SOURCE_CODE_MODE_HOST=$(find "$AGENTROUTE_CODE_MODE_HOST_TMP/package/vendor" \
            -type f -name codex-code-mode-host -perm -111 -print -quit)
    fi
    if [ ! -x "$AGENTROUTE_SOURCE_CODE_MODE_HOST" ]; then
        printf 'No executable Code Mode host was found.\n' >&2
        exit 1
    fi
    CARGO_TARGET_DIR="$AGENTROUTE_CODEX_TARGET" \
    CARGO_INCREMENTAL=0 \
    CARGO_BUILD_JOBS=${CARGO_BUILD_JOBS:-2} \
        cargo build --manifest-path "$AGENTROUTE_CODEX_SOURCE/codex-rs/Cargo.toml" \
        --locked --profile "$AGENTROUTE_BUILD_PROFILE" \
        -p codex-cli --bin codex

    AGENTROUTE_STAGED_CODEX="$AGENTROUTE_CODEX_TARGET/$AGENTROUTE_BUILD_PROFILE/codex-agentroute-staged"
    cp "$AGENTROUTE_CODEX_TARGET/$AGENTROUTE_BUILD_PROFILE/codex" "$AGENTROUTE_STAGED_CODEX"
    chmod 755 "$AGENTROUTE_STAGED_CODEX"
    if [ "$(uname -s)" = Darwin ]; then
        # Sign and verify the staged file before it can replace the live binary.
        # An interrupted installer therefore leaves the previous runtime intact.
        codesign --force --deep --sign - "$AGENTROUTE_STAGED_CODEX"
        codesign --verify --deep --strict "$AGENTROUTE_STAGED_CODEX"
    fi
    "$AGENTROUTE_STAGED_CODEX" --version >/dev/null

    "$AGENTROUTE_HOME_DIR/venv/bin/python" -c '
from pathlib import Path
from agentroute.codex_patch import install_binary
install_binary(
    Path("'"$AGENTROUTE_STAGED_CODEX"'"),
    Path("'"$AGENTROUTE_BIN_DIR/codex-bin"'"),
)
'
    rm -f "$AGENTROUTE_STAGED_CODEX"
    if [ "$AGENTROUTE_SOURCE_CODE_MODE_HOST" != "$AGENTROUTE_BIN_DIR/codex-code-mode-host" ]; then
        cp "$AGENTROUTE_SOURCE_CODE_MODE_HOST" "$AGENTROUTE_BIN_DIR/codex-code-mode-host"
    fi
    chmod 755 "$AGENTROUTE_BIN_DIR/codex-bin" "$AGENTROUTE_BIN_DIR/codex-code-mode-host"
    printf '%s\n' "$AGENTROUTE_BUILD_ID" >"$AGENTROUTE_BUILD_ID_FILE"
    if [ "${AGENTROUTE_KEEP_BUILD:-0}" != 1 ]; then
        rm -rf "$AGENTROUTE_CODEX_TARGET"
    fi
    if [ -n "$AGENTROUTE_CODE_MODE_HOST_TMP" ]; then
        rm -rf "$AGENTROUTE_CODE_MODE_HOST_TMP"
    fi
fi

if [ "$(uname -s)" = Darwin ]; then
    # Re-sign even when the compiled build ID is unchanged so installer-only
    # entitlement fixes repair an existing helper without rebuilding Codex.
    codesign --force --deep --sign - \
        --entitlements "$AGENTROUTE_PROJECT_ROOT/assets/desktop/codex-code-mode-host.entitlements.plist" \
        "$AGENTROUTE_BIN_DIR/codex-code-mode-host"
fi
"$AGENTROUTE_HOME_DIR/venv/bin/python" \
    "$AGENTROUTE_PROJECT_ROOT/src/agentroute/code_mode_smoke.py" \
    "$AGENTROUTE_BIN_DIR/codex-code-mode-host"

if [ -n "$AGENTROUTE_STOCK_CODEX" ] && [ "$AGENTROUTE_STOCK_CODEX" != "$AGENTROUTE_BIN_DIR/codex" ]; then
    ln -sf "$AGENTROUTE_STOCK_CODEX" "$AGENTROUTE_BIN_DIR/codex-stock"
fi

cp "$AGENTROUTE_PROJECT_ROOT/packaging/codex-launcher" "$AGENTROUTE_BIN_DIR/codex"
chmod 755 "$AGENTROUTE_BIN_DIR/codex"

if [ "${AGENTROUTE_INSTALL_ACTIVATE:-1}" = 1 ]; then
    "$AGENTROUTE_BIN_DIR/agentroute" setup

    AGENTROUTE_SHELL_RC=${ZDOTDIR:-"$HOME"}/.zshrc
    AGENTROUTE_PATH_LINE='export PATH="$HOME/.agentroute/bin:$PATH" # agentroute'
    if ! grep -F "$AGENTROUTE_PATH_LINE" "$AGENTROUTE_SHELL_RC" >/dev/null 2>&1; then
        printf '\n%s\n' "$AGENTROUTE_PATH_LINE" >>"$AGENTROUTE_SHELL_RC"
    fi

    printf '\nAgentRoute installed in %s\n' "$AGENTROUTE_HOME_DIR"
    printf 'Run: source %s\n' "$AGENTROUTE_SHELL_RC"
    printf 'Then launch Codex normally: codex\n'
    printf 'Original Codex remains available as: codex-stock\n'
else
    printf '\nAgentRoute build payload prepared in %s\n' "$AGENTROUTE_HOME_DIR"
fi
