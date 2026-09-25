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

The fork is the development surface; the AgentRoute installer still consumes the pinned
upstream commit and packaged patch stack until that build pipeline is explicitly migrated.
Do not change the pin, publish a release, or replace an installed binary merely because a
rebase completed. Regenerate and verify patch exports, record the exact source/build identity,
and follow [the release gate](releasing.md).

The 0.157.0 candidate is `00c972ed5d6ff6499317fd41b7f23605b8e6850d`
(`rust-v0.157.0`). It is separate from the newer mirrored main. The initial port is not a
release until its compilation, regression checks, and runtime smoke tests pass.
