# Maintaining the Codex fork

The downstream fork is `atiti/codex`; upstream is `openai/codex`.

## Branch ownership

- `main`: pristine upstream mirror. No AgentRoute commits or release-version rewrites.
- `agentroute`: downstream development branch. Rebase our changes onto a reviewed `main`
  update; tracking a branch does not automatically incorporate its commits.
- `agentroute-release-<version>`: stable-release ports, based on the exact upstream release
  commit. Upstream `main` can be substantially ahead of a stable release.
- `archive/main-before-agentroute-2026-09-25`: preserved old fork main at
  `140cde7376247ae3c6e3cf3792f01c995cb7023a`, including its two remote-session commits.

On September 25, the fork main was synchronized to upstream commit
`d7b07d45517a793acfba4cbf8de697d723cceb46`. The initial synchronization required an
exact-SHA force-with-lease after verifying the archive because the old main had diverged.
Future normal main updates must fast-forward; unexpected divergence is a stop condition.

## Safe update cycle

For a normal clone with `origin` pointing to the fork and `upstream` to OpenAI:

```sh
git fetch upstream main --tags
git fetch origin
git switch main
git merge --ff-only upstream/main
git push origin main
git switch agentroute
git branch archive/agentroute-before-update
git rebase main
# Review conflicts, run focused tests, and validate routing and provider state.
git push --force-with-lease origin agentroute
```

Use a unique archive branch name for each update. Rebase only with a clean worktree, and
coordinate before rewriting a shared development branch. Never force-push `main` as part of
routine synchronization. A stable release port should start from its release tag instead of
following unreleased `main` commits.

Some existing source-build checkouts use `origin` for OpenAI and `fork` for `atiti/codex`.
Check `git remote -v` before using the example commands. If a local `agentroute` branch
tracks `fork/main` with rebase-on-pull enabled, push explicitly with
`git push fork HEAD:agentroute`; do not rely on an implicit push destination when the
tracked branch and development branch have different names.

## Release boundary

The fork is the authoritative source for new builds. `scripts/install.sh` pins the exact
downstream commit, its upstream ancestor, and the matching official Code Mode host version.
It uses a separate `src/codex-stack` checkout and refuses dirty source trees. The older
`src/codex` checkout is preserved. Legacy packaged patches remain available only for their
old exact upstream base; they are not applied on top of the new fork stack.

Do not change the pin, publish a release, or replace an installed binary merely because a
rebase completed. Record the exact source/build identity and follow
[the release gate](releasing.md). The scheduled compatibility check now rebases the ordered
stack into a fresh worktree; it never installs or publishes a release automatically.

The 0.157.0 candidate is `00c972ed5d6ff6499317fd41b7f23605b8e6850d`
(`rust-v0.157.0`). It is separate from the newer mirrored main. The initial port is not a
release until its compilation, regression checks, and runtime smoke tests pass.

## Ordered 0.157 stack and subsequent ports

`agentroute-stack-0.157` is the reviewable 20-commit stack, ending at
`c6833a2bc48776d002da147bbccf62529878c1c5`. The current installer pin
`0d2fa6eb996cf0b643e9ed993cf3d8e925fbc5cb` adds the Azure Direct
`access_programs` guard on top of that stack. Its final tree is the
validated initial port plus the TUI status-line fix that keeps the routed
model and provider visible when a route notice omits the effort field, and the
status-row fix that lets a routed provider's streamed limit family appear in
`/status` while Codex families stay owned by the account read, with the API-side
contract test that pins the routed header family. The final stack commit removes Codex identity
claims from the request copy sent to every provider while keeping saved session
instructions unchanged. The earlier history remains available as
`archive/agentroute-0.157-before-split-2026-09-25`; no published branch was rewritten
to produce the split stack.

Commits separate lockfiles, protocol ownership, hook contracts, generated schemas,
provider capabilities, request-state normalization, turn activation, pre-compaction
routing, compaction, child routing, tool adaptation, reviewer fallback, hook runtime,
tracing, TUI presentation, history recovery, and documentation. Apply the entire
ordered stack: neighboring API-definition and caller commits are not independently
buildable releases. Do not squash it into one routing patch.

After fetching the next release tag, prepare a new candidate without modifying the
working installation or published branches:

```sh
python3 scripts/codex_stack.py /path/to/codex \
  --base 00c972ed5d6ff6499317fd41b7f23605b8e6850d \
  --tip 0d2fa6eb996cf0b643e9ed993cf3d8e925fbc5cb \
  --onto rust-vNEXT \
  --branch agentroute-port-NEXT \
  --worktree /path/to/new-candidate
```

The helper resolves refs to commits, validates ancestry, requires a new branch and
worktree, enables Git's conflict-resolution reuse, and prints a `range-diff` command.
Conflicts remain in that isolated worktree for review. It never pushes or installs.
After resolving, keep each fix with its relevant feature using fixup/autosquash in
the unpublished candidate. Compare the old and new stacks with `range-diff` before
publishing a new versioned branch.

Validate the complete stack with formatting, `cargo check -p codex-cli`, focused
core/hook/TUI regressions, a CLI build, a version-matched Code Mode host smoke test,
and live provider tests. A green rebase alone does not establish runtime compatibility.
Keep the previous installed binary until candidate validation passes.

The local test entry point is `codex-0157`, installed separately from `codex` with
its matching host under `~/.agentroute/candidates/codex-0.157`. It uses the same
Codex home, hooks, provider credentials, and account routing. It is a test build,
not a public signed/notarized release; the normal CLI and Desktop remain intact.

## Desktop 0.158 port (AgentRoute 0.5.57)

The release pin is `e17c80b71da526e083043c9a0055cbe9b9279376` on
`codex/desktop-0158-runtime`, based on the exact Desktop release
`0d9c7cbfa6cf1489f55a8a9542b75ddd2c061807` (`rust-v0.158.0-alpha.2.1`).
All 24 routing-stack commits were ported. The substantive conflict preserves
upstream's inherited agent-control initialization and downstream's isolated reviewer
credentials. Lockfile workspace versions now match upstream; child spawning imports
the upstream AgentControl trait. The native CLI version is unchanged.

## Goal routing (runtime v48)

Development source pins `af578870da6c605a5d9f5648d295d7769668070b`
(`codex/goal-routing`), based on the v47 Desktop-compatible fork.

The `/goal` command stores an objective and starts autonomous work. Its internal
continuations previously bypassed `UserPromptSubmit`, so they inherited whichever
model the task already used. The goal-aware runtime supplies the persisted objective
and goal ID to AgentRoute before sampling.

- The first autonomous turn classifies the objective using the configured classifier
  (including hosted JEV), with the usual rule and capability checks.
- Editing the objective or replacing the goal triggers a fresh classification.
- Later continuations reuse the active route tier, backend, and reasoning effort;
  they do not pay for another objective classification. This state survives restart.
- Capacity checks still run every turn. Reusing a route does not bypass quotas.
- Objective tags apply on activation. A later explicit prompt such as `@normal continue`
  or `@claude @smart continue` changes the active route for subsequent continuations.

For example, set `/goal @smart Implement the migration and verify rollback` to opt
into SMART immediately, or omit the tag to classify the objective automatically.
The goal payload is routing metadata, not an extra user message in the model history.

`agentroute history --limit 10` lists routing decisions; `agentroute explain <ID>`
shows the goal ID and `classify_objective` or `continuation` mode. Each goal turn's
stored selection receipt also includes the objective hash.
The continuity table stores the hash rather than an additional copy of the objective.
This requires both the goal-aware fork and companion AgentRoute hook; installing only
one side does not enable objective classification. AgentRoute 0.5.58/runtime v49
includes both.

## Stable 0.159.2 port (AgentRoute 0.5.58)

The release pin is `550d7b423e57a2d4a60f72302c25e8f7051f44cf` on
`agentroute-release-0.159.2`, based on OpenAI's stable `rust-v0.159.2`
(`ff6aec96948b70d94983af2641a6b67c94faeff5`). The port retains the 26
ordered routing commits and repairs upstream changes to provider session state,
Guardian review, and input metadata. Its native CLI version and Code Mode host
are both 0.159.2.


## Stable 0.160.0 port (AgentRoute 0.5.63 / runtime v50)

The release pin is `2385f6b58e3d380f10d5f2d8d5d13d4c65ef1021` on
`agentroute-release-0.160.0`, based on `rust-v0.160.0`
(`a956835d020762cb2b570053af06f643a11c0ecc`). All 27 existing patch commits
were replayed; range-diff showed only changed upstream context for the TUI patch.
Native Codex and the official Code Mode host both use 0.160.0.

The stack also includes upstream attachment reverse lookup (#50083 and #50094),
regenerated protocol exports, workspace lock alignment, and a shared raw trace
budget. The quota lock is held until the file write completes, and pruning uses
the same lock to reconcile the budget. Trace errors remain best-effort diagnostics.

Validation: 849 tests across rollout-trace, state, thread-store and app-server
protocol; focused routed-provider core tests; scoped Clippy; schema regeneration
including the Python SDK using Python 3.12; required Bazel lock refresh.

## Stable 0.161.0 candidate (AgentRoute 0.5.67 / runtime v52)

The candidate pin is `bd9c95949c745e8f070b45c48795b0355e6c142b` on
`codex/agentroute-stable-0.161.0`, based on OpenAI's stable `rust-v0.161.0`
(`979011409de0a60b52f179721948e65531d26144`). The complete downstream stack was
ported in order. Follow-up commits finish API/lock/schema alignment and protect
standalone compaction and resume warmup from replaying encrypted state owned by
another provider. Resume prewarm history is bounded by serialized bytes; legacy
rollouts without provider metadata remain resumable. Stored history is preserved.

Native Codex and the official Code Mode host use 0.161.0. The companion shared
server bootstrap raises only its child's open-file soft limit, to at most 8192
within the inherited hard limit. It does not change machine or parent limits.

The launcher identifies its verified local Unix socket with
`AGENTROUTE_LOCAL_SERVER_SOCKET`. The TUI uses local repository discovery and
folder consent for that exact endpoint, including saved folders on resume and
fork. Shared task discovery and server management keep their existing behavior.
Explicit remote launches clear the marker. Trust entries are changed only after
the user confirms the normal folder-consent prompt.

Validation includes the CLI build, 319 protocol tests, 185 hook tests, ten focused
provider-state/reviewer/warmup tests, scoped Clippy, stable and experimental app-server
exports, hook schema regeneration, formatting, and the required Bazel lock refresh.
Daybreak Blue, GPT subscription, Azure and DeepSeek native turns passed, including a mixed-provider tool/resume session and plain/explicit repository folder consent. Windows host/target CRT policy and V8 initialization are aligned; 77 build-script checks and 83 V8 runtime/POC checks pass. Provider footer snapshots were reviewed and targeted Guardian, recovery and folder-consent checks pass. The existing same-provider websocket cache reuse test remains green. The AgentRoute
shared-client integration exercises Desktop stdio, terminal/mobile RPC, history,
and ownership on an isolated temporary profile.

This candidate is separate from the active installation. Do not replace a running
shared owner or Desktop while other tasks are active. Full live provider and Desktop
acceptance remains part of the release gate before a public tag is published.
