#!/bin/sh
set -eu

AGENTROUTE_PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
AGENTROUTE_HOME_DIR=${AGENTROUTE_HOME:-"$HOME/.agentroute"}
AGENTROUTE_BIN_DIR="$AGENTROUTE_HOME_DIR/bin"
AGENTROUTE_CODEX_SOURCE="$AGENTROUTE_HOME_DIR/src/codex"
AGENTROUTE_CODEX_TARGET="$AGENTROUTE_HOME_DIR/build/codex"
AGENTROUTE_BUILD_PROFILE=${AGENTROUTE_BUILD_PROFILE:-dev-small}
AGENTROUTE_CODEX_COMMIT=b0af519c39766c173191fc39b341808619b51c74
AGENTROUTE_BUILD_ID="$AGENTROUTE_CODEX_COMMIT-route-notice-code-mode-v1"
AGENTROUTE_BUILD_ID_FILE="$AGENTROUTE_HOME_DIR/build-id"
AGENTROUTE_PATCH="$AGENTROUTE_PROJECT_ROOT/patches/codex-user-prompt-model-override.patch"

for command_name in git cargo uv; do
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

AGENTROUTE_SOURCE_CODE_MODE_HOST=${AGENTROUTE_CODE_MODE_HOST:-}
if [ -z "$AGENTROUTE_SOURCE_CODE_MODE_HOST" ] && [ -n "$AGENTROUTE_STOCK_CODEX" ]; then
    AGENTROUTE_STOCK_BIN_DIR=$(CDPATH= cd -- "$(dirname -- "$AGENTROUTE_STOCK_CODEX")" && pwd)
    if [ -x "$AGENTROUTE_STOCK_BIN_DIR/codex-code-mode-host" ]; then
        AGENTROUTE_SOURCE_CODE_MODE_HOST="$AGENTROUTE_STOCK_BIN_DIR/codex-code-mode-host"
    else
        AGENTROUTE_STOCK_PREFIX=$(dirname -- "$AGENTROUTE_STOCK_BIN_DIR")
        AGENTROUTE_SOURCE_CODE_MODE_HOST=$(find \
            "$AGENTROUTE_STOCK_PREFIX/lib/node_modules/@openai/codex" \
            -type f -name codex-code-mode-host -perm -111 -print -quit 2>/dev/null || true)
    fi
fi

if [ ! -x "$AGENTROUTE_HOME_DIR/venv/bin/python" ]; then
    uv venv --python 3.12 "$AGENTROUTE_HOME_DIR/venv"
fi
uv pip install --python "$AGENTROUTE_HOME_DIR/venv/bin/python" "$AGENTROUTE_PROJECT_ROOT"
ln -sf "$AGENTROUTE_HOME_DIR/venv/bin/agentroute" "$AGENTROUTE_BIN_DIR/agentroute"

if [ ! -d "$AGENTROUTE_CODEX_SOURCE/.git" ]; then
    git clone --filter=blob:none --no-checkout https://github.com/openai/codex.git "$AGENTROUTE_CODEX_SOURCE"
fi

git -C "$AGENTROUTE_CODEX_SOURCE" fetch origin "$AGENTROUTE_CODEX_COMMIT"
git -C "$AGENTROUTE_CODEX_SOURCE" checkout --detach "$AGENTROUTE_CODEX_COMMIT"
if git -C "$AGENTROUTE_CODEX_SOURCE" apply --reverse --check "$AGENTROUTE_PATCH" >/dev/null 2>&1; then
    printf 'Codex patch is already applied.\n'
else
    git -C "$AGENTROUTE_CODEX_SOURCE" apply --check "$AGENTROUTE_PATCH"
    git -C "$AGENTROUTE_CODEX_SOURCE" apply "$AGENTROUTE_PATCH"
fi

if [ ! -x "$AGENTROUTE_BIN_DIR/codex-bin" ] \
    || [ ! -x "$AGENTROUTE_BIN_DIR/codex-code-mode-host" ] \
    || [ "$AGENTROUTE_INSTALLED_BUILD_ID" != "$AGENTROUTE_BUILD_ID" ]; then
    CARGO_TARGET_DIR="$AGENTROUTE_CODEX_TARGET" \
    CARGO_INCREMENTAL=0 \
    CARGO_BUILD_JOBS=${CARGO_BUILD_JOBS:-2} \
        cargo build --manifest-path "$AGENTROUTE_CODEX_SOURCE/codex-rs/Cargo.toml" \
        --profile "$AGENTROUTE_BUILD_PROFILE" \
        -p codex-cli --bin codex

    cp "$AGENTROUTE_CODEX_TARGET/$AGENTROUTE_BUILD_PROFILE/codex" "$AGENTROUTE_BIN_DIR/codex-bin"
    if [ "$(uname -s)" = Darwin ]; then
        codesign --force --sign - "$AGENTROUTE_BIN_DIR/codex-bin"
    fi
    if [ -z "$AGENTROUTE_SOURCE_CODE_MODE_HOST" ]; then
        printf 'No compatible Code Mode host was found beside the stock Codex installation.\n' >&2
        printf 'Set AGENTROUTE_CODE_MODE_HOST to the host executable and rerun.\n' >&2
        exit 1
    fi
    "$AGENTROUTE_SOURCE_CODE_MODE_HOST" --help >/dev/null
    cp "$AGENTROUTE_SOURCE_CODE_MODE_HOST" "$AGENTROUTE_BIN_DIR/codex-code-mode-host"
    chmod 755 "$AGENTROUTE_BIN_DIR/codex-bin" "$AGENTROUTE_BIN_DIR/codex-code-mode-host"
    printf '%s\n' "$AGENTROUTE_BUILD_ID" >"$AGENTROUTE_BUILD_ID_FILE"
    if [ "${AGENTROUTE_KEEP_BUILD:-0}" != 1 ]; then
        rm -rf "$AGENTROUTE_CODEX_TARGET"
    fi
fi

if [ -n "$AGENTROUTE_STOCK_CODEX" ] && [ "$AGENTROUTE_STOCK_CODEX" != "$AGENTROUTE_BIN_DIR/codex" ]; then
    ln -sf "$AGENTROUTE_STOCK_CODEX" "$AGENTROUTE_BIN_DIR/codex-stock"
fi

printf '#!/bin/sh\nexec "%s/bin/codex-bin" --enable step_model_switching --enable code_mode -c suppress_unstable_features_warning=true "$@"\n' \
    "$AGENTROUTE_HOME_DIR" >"$AGENTROUTE_BIN_DIR/codex"
chmod 755 "$AGENTROUTE_BIN_DIR/codex"

if [ -f "$AGENTROUTE_HOME_DIR/config.yaml" ]; then
    "$AGENTROUTE_BIN_DIR/agentroute" enable
else
    "$AGENTROUTE_BIN_DIR/agentroute" init --enable
fi
"$AGENTROUTE_BIN_DIR/agentroute" install-hook

AGENTROUTE_SHELL_RC=${ZDOTDIR:-"$HOME"}/.zshrc
AGENTROUTE_PATH_LINE='export PATH="$HOME/.agentroute/bin:$PATH" # agentroute'
if ! grep -F "$AGENTROUTE_PATH_LINE" "$AGENTROUTE_SHELL_RC" >/dev/null 2>&1; then
    printf '\n%s\n' "$AGENTROUTE_PATH_LINE" >>"$AGENTROUTE_SHELL_RC"
fi

printf '\nAgentRoute installed in %s\n' "$AGENTROUTE_HOME_DIR"
printf 'Run: source %s\n' "$AGENTROUTE_SHELL_RC"
printf 'Then launch Codex normally: codex\n'
printf 'Original Codex remains available as: codex-stock\n'
