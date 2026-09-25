#!/bin/sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
UPSTREAM_REF=${1:-rust-v0.157.0}
CHECK_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/agentroute-upstream.XXXXXX")
# Keep candidate worktrees for diagnostics instead of deleting conflict evidence.
trap 'printf "Candidate worktree retained at %s\n" "$CHECK_ROOT"' EXIT
CODEX_TIP=$(sed -n 's/^AGENTROUTE_CODEX_COMMIT=//p' "$PROJECT_ROOT/scripts/install.sh")
CODEX_BASE=$(sed -n 's/^AGENTROUTE_CODEX_UPSTREAM=//p' "$PROJECT_ROOT/scripts/install.sh")
git clone --filter=blob:none --no-checkout https://github.com/atiti/codex.git "$CHECK_ROOT/repo"
git -C "$CHECK_ROOT/repo" fetch origin "$CODEX_TIP"
git -C "$CHECK_ROOT/repo" fetch https://github.com/openai/codex.git "$UPSTREAM_REF"
NEW_BASE=$(git -C "$CHECK_ROOT/repo" rev-parse FETCH_HEAD)
git -C "$CHECK_ROOT/repo" config user.name 'AgentRoute port check'
git -C "$CHECK_ROOT/repo" config user.email 'agentroute-port@example.invalid'
python3 "$PROJECT_ROOT/scripts/codex_stack.py" "$CHECK_ROOT/repo" \
    --base "$CODEX_BASE" --tip "$CODEX_TIP" --onto "$NEW_BASE" \
    --branch agentroute-port-ci --worktree "$CHECK_ROOT/candidate"
git -C "$CHECK_ROOT/candidate" diff --check "$NEW_BASE" HEAD
if [ "$NEW_BASE" = "$CODEX_BASE" ]; then
    test "$(git -C "$CHECK_ROOT/candidate" rev-parse 'HEAD^{tree}')" = \
        "$(git -C "$CHECK_ROOT/repo" rev-parse "$CODEX_TIP^{tree}")"
fi

if [ "${AGENTROUTE_UPSTREAM_CARGO_CHECK:-1}" = 1 ]; then
    cargo check --locked --manifest-path "$CHECK_ROOT/candidate/codex-rs/Cargo.toml" -p codex-cli
fi
printf 'AgentRoute commit stack validated against OpenAI Codex %s.\n' \
    "$UPSTREAM_REF"
