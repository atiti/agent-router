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

`agentroute-stack-0.157` is the reviewable 17-commit stack, ending at
`90f76f2013f028b3f9bd3a587bf151cb4934bcb4`. Its final tree is identical to the
validated initial port. The earlier history remains available as
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
  --tip 90f76f2013f028b3f9bd3a587bf151cb4934bcb4 \
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
