#!/bin/sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
UPSTREAM_REF=${1:-main}
CHECK_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/agentroute-upstream.XXXXXX")
cleanup() {
    rm -rf "$CHECK_ROOT"
}
trap cleanup EXIT HUP INT TERM

git clone --filter=blob:none --no-checkout https://github.com/openai/codex.git "$CHECK_ROOT/codex"
git -C "$CHECK_ROOT/codex" fetch --depth 1 origin "$UPSTREAM_REF"
git -C "$CHECK_ROOT/codex" checkout --detach FETCH_HEAD
for patch in \
    "$PROJECT_ROOT/patches/codex-user-prompt-model-override.patch" \
    "$PROJECT_ROOT/patches/codex-package-version.patch" \
    "$PROJECT_ROOT/patches/codex-history-recovery.patch" \
    "$PROJECT_ROOT/src/agentroute/patches/codex-route-application-receipt.patch"
do
    git -C "$CHECK_ROOT/codex" apply --recount --check "$patch"
    git -C "$CHECK_ROOT/codex" apply --recount "$patch"
done

if [ "${AGENTROUTE_UPSTREAM_CARGO_CHECK:-1}" = 1 ]; then
    cargo check --manifest-path "$CHECK_ROOT/codex/codex-rs/Cargo.toml" -p codex-cli
fi
printf 'AgentRoute patches and codex-cli check passed against OpenAI Codex %s.\n' \
    "$UPSTREAM_REF"
