#!/bin/sh
set -eu

PAYLOAD_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENTROUTE_HOME_DIR=${AGENTROUTE_HOME:-"$HOME/.agentroute"}
AGENTROUTE_BIN_DIR="$AGENTROUTE_HOME_DIR/bin"

if ! command -v uv >/dev/null 2>&1; then
    printf 'Missing uv. Install it from https://docs.astral.sh/uv/ and retry.\n' >&2
    exit 1
fi
for required in codex-bin codex-code-mode-host codex-launcher; do
    if [ ! -f "$PAYLOAD_ROOT/$required" ]; then
        printf 'Release payload is incomplete: missing %s\n' "$required" >&2
        exit 1
    fi
done

mkdir -p "$AGENTROUTE_BIN_DIR"
STOCK_CODEX=$(command -v codex 2>/dev/null || true)
if [ -n "$STOCK_CODEX" ] && [ "$STOCK_CODEX" != "$AGENTROUTE_BIN_DIR/codex" ]; then
    ln -sf "$STOCK_CODEX" "$AGENTROUTE_BIN_DIR/codex-stock"
fi

if [ ! -x "$AGENTROUTE_HOME_DIR/venv/bin/python" ]; then
    uv venv --python 3.12 "$AGENTROUTE_HOME_DIR/venv"
fi
WHEEL=
for candidate in "$PAYLOAD_ROOT"/agentroute-*.whl; do
    if [ -f "$candidate" ]; then
        WHEEL=$candidate
        break
    fi
done
if [ -z "$WHEEL" ]; then
    printf 'Release payload is incomplete: missing AgentRoute wheel\n' >&2
    exit 1
fi
uv pip install --python "$AGENTROUTE_HOME_DIR/venv/bin/python" --upgrade "$WHEEL"

cp "$PAYLOAD_ROOT/codex-bin" "$AGENTROUTE_BIN_DIR/codex-bin"
cp "$PAYLOAD_ROOT/codex-code-mode-host" "$AGENTROUTE_BIN_DIR/codex-code-mode-host"
cp "$PAYLOAD_ROOT/codex-launcher" "$AGENTROUTE_BIN_DIR/codex"
cp "$PAYLOAD_ROOT/build-id" "$AGENTROUTE_HOME_DIR/build-id"
ln -sf "$AGENTROUTE_HOME_DIR/venv/bin/agentroute" "$AGENTROUTE_BIN_DIR/agentroute"
chmod 755 "$AGENTROUTE_BIN_DIR/codex" "$AGENTROUTE_BIN_DIR/codex-bin" \
    "$AGENTROUTE_BIN_DIR/codex-code-mode-host"

"$AGENTROUTE_BIN_DIR/agentroute" setup
case "${SHELL:-}" in
    */zsh) SHELL_RC=${ZDOTDIR:-"$HOME"}/.zshrc ;;
    *) SHELL_RC="$HOME/.profile" ;;
esac
PATH_LINE='export PATH="$HOME/.agentroute/bin:$PATH" # agentroute'
touch "$SHELL_RC"
if ! grep -F "$PATH_LINE" "$SHELL_RC" >/dev/null 2>&1; then
    printf '\n%s\n' "$PATH_LINE" >>"$SHELL_RC"
fi

printf 'AgentRoute installed from a verified release in %s\n' "$AGENTROUTE_HOME_DIR"
printf 'Run: source %s\n' "$SHELL_RC"
printf 'Then: agentroute doctor && codex\n'
