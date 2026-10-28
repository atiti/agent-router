# Experimental progress review

The Codex `progress_review` feature reviews direction during a long-running root turn, including ordinary prompts without a Goal. It defaults off. Start an AgentRoute-managed Codex session with:

```sh
codex --enable progress_review
```

Disable it with `--disable progress_review`. The selected Codex build must include the feature; an older installed binary will not recognize it. Existing sessions keep their original runtime configuration.

The reviewer runs after ten minutes of active work, accumulating across turns; idle time is excluded. Three consecutive tool failures, input usage reaching 80% of a known context window, or an agent or skill calling `request_progress_review` may trigger it sooner after a two-minute cooldown. It is isolated, read-only and bounded by the same two-minute timeout as the completion reviewer. It inherits the current turn's model and provider; it does not independently select a different AgentRoute tier. A verdict is advice to the working agent, not permission to mutate files or cancel a process. History is bounded and may omit earlier context. The clock and previous verdict are saved under the Codex home by thread ID at turn boundaries and reviews; work since the last save may be lost on a crash. The working agent should use the `goal-checkpoint` skill to keep the outcome and constraints clear.

When a completed review exposes usage and its item is present in the parent rollout, AgentRoute's execution receipt records aggregate reviewer input, cached input, and output tokens separately. The receipt also records how many reviews have observable usage; missing usage and truncated or legacy rollout tails remain unmeasured. It does not estimate cost or include reviewer text. Internal reviews skip the normal AgentRoute subagent routing hooks.

The AgentRoute installer installs `goal-checkpoint` into the active Codex home when activating. It preserves an existing skill of the same name; update a customized or older copy manually if needed. Start a new session to refresh the skill catalog.
