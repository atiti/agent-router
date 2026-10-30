# Experimental progress review

The Codex `progress_review` feature reviews direction during a long-running root turn, including ordinary prompts without a Goal. It defaults off. Start an AgentRoute-managed Codex session with:

```sh
codex --enable progress_review
```

For a noninteractive task, use `codex exec --enable progress_review "your task"`. AgentRoute preserves routing and Code Mode defaults when feature flags follow the subcommand.

Disable it with `--disable progress_review`. The selected Codex build must include the feature; an older installed binary will not recognize it. Existing sessions keep their original runtime configuration.

The reviewer runs after ten minutes of active work, accumulating across turns; idle time is excluded. Three consecutive tool failures, input usage reaching 80% of a known context window, or an agent or skill calling `request_progress_review` may trigger it sooner after a two-minute cooldown. It is isolated, read-only and bounded by the same two-minute timeout as the completion reviewer. By default it inherits the current model and provider. To select a separate AgentRoute backend and tier, set `routing.progress_review_backend` and optionally `routing.progress_review_tier` in AgentRoute's configuration. The backend must be enabled; the tier defaults to `fast`. Managed provider restrictions still apply. Shared-server sessions require a fresh server configuration. A verdict is advice to the working agent, not permission to mutate files or cancel a process. History is bounded and may omit earlier context. The clock and previous verdict are saved under the Codex home by thread ID at turn boundaries and reviews; work since the last save may be lost on a crash. The working agent should use the `goal-checkpoint` skill to keep the outcome and constraints clear.

When a completed review exposes usage and its item is present in the parent rollout, AgentRoute's execution receipt records reviewer tokens and the session's model and provider without retaining review text. Usage analytics estimate cost where configured model prices and cache-write usage are available; missing usage, unknown prices, and truncated or legacy rollout tails remain unmeasured. Internal reviews skip the normal AgentRoute subagent routing hooks.

The source and published-release installers install `goal-checkpoint` into the active Codex home when activating. They preserve an existing skill of the same name; update a customized or older copy manually if needed. Start a new session to refresh the skill catalog.

## Live escalation within an active task

Enable this in AgentRoute's YAML configuration, then start a fresh session with `codex --enable progress_review`:

```yaml
routing:
  progress_review_live_switching: true
```

A shared server needs a safe stop/start to load these settings. Requires a Codex build containing the live-review bridge.

The reviewer may recommend `model_tier` (`keep`, `fast`, `normal`, `smart`, `max`). AgentRoute generates an allowlist from enabled backends and its maximum-tier policy. Codex applies an authorized escalation to subsequent model steps of the **same active turn**; it does not wait for a new user prompt or cancel a running tool. Model/provider compatibility and managed provider requirements use the existing routed-settings path.

Switching defaults off. Explicit CLI model selection disables the generated allowlist. Budgeted backends are excluded until a live capacity check is available. Downgrades and unknown current models remain advisory. Stale user revisions are rejected at publication. The existing review cooldown limits switching frequency, and route results are recorded in the visible review transcript. This initial implementation controls the reviewed root turn, not already-running child agents. Disable with `routing.progress_review_live_switching: false` and start a fresh session.
