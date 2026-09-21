# Changelog

## Unreleased

- Improve the open-source project surface with a visual README quickstart, support and conduct
  guidance, structured issue and pull-request templates, automated dependency updates, and an
  OpenSSF Scorecard workflow.

## 0.5.32 — 2026-09-20

- Match the routed Codex CLI package version to the CLI embedded in the current official Desktop
  app, preserving the Desktop/mobile compatibility gate without forcing a mismatch.
- Show the active model provider beside the model in the configurable CLI status line, including
  per-turn AgentRoute backend changes parsed from the visible route notice.
- When an automatic approval reviewer exhausts its ChatGPT subscription, retry the reviewer only
  on a distinct healthy signed-in profile, keep the answer route unchanged, re-key reviewer state,
  and show the profile switch in a visible fallback banner.

## 0.5.31 — 2026-09-20

- Fix the maintained Codex patch's startup provider-compatibility call to pass the optional
  compatibility value expected by the shared helper, restoring cross-platform release builds.

## 0.5.30 — 2026-09-20

- Add per-ChatGPT-profile tier targets so accounts with different model entitlements can share the
  same AgentRoute installation; for example, personal MAX can use Astra while a work profile uses
  Sol.
- Resolve the current profile from `CODEX_HOME` or a locally hashed account identity on every GPT
  turn, and apply the destination profile's model target during in-thread capacity failover.
- Add `agentroute capacity profile-model` to set or clear profile-specific model compatibility
  without hand-editing YAML, while keeping raw account IDs out of configuration and audit output.

## 0.5.29 — 2026-09-20

- Preserve the parent turn's Guardian and Node REPL safety policy for every routed provider,
  including fully tool-compatible Azure deployments.
- Add `agentroute capacity profile-bootstrap` to copy reusable hooks, MCP configuration, rules,
  and skills into an isolated profile without copying authentication or session state.
- Stage, sign, verify, and smoke-test a rebuilt macOS Codex executable before atomically replacing
  the live binary, so an interrupted source install cannot leave a taskgated-killed runtime.

## 0.5.28 — 2026-09-19

- Continue an existing Codex CLI or Desktop thread on another configured ChatGPT subscription
  profile when the active profile is authoritatively exhausted or unavailable.
- Keep profile selection, priority, capacity policy, session affinity, audit metadata, and visible
  `PROFILE FAILOVER` / `PROFILE ROUTE` notices in AgentRoute; the maintained Codex patch only accepts
  a turn-scoped profile home and constructs a turn-local authenticated provider.
- Strip provider-bound encrypted reasoning and cache state on every alternate-profile turn while
  preserving portable conversation and tool history; never replay an interrupted or failed turn.
- Refuse to switch on unknown quota telemetry, skip signed-out/disabled/unavailable profiles, and
  avoid treating two profile homes for the same ChatGPT account as independent capacity.

## 0.5.27 — 2026-09-19

- Add opt-in capacity management: authoritative ChatGPT subscription telemetry, audited
  daily/monthly API-equivalent budgets, bounded backend preference-ring failover, recovery
  hysteresis, explicit-route fail-closed behavior, and visible warning/fallback/block messages.
- Support named, isolated `CODEX_HOME` subscription profiles for launch-time selection.
- Propagate app-server `ordinaryUsageAllowed` into active Codex hook sessions without a per-turn
  network request, and preserve a known exhausted state across sparse rate-limit updates.
- Add capacity status, profile probing, budgets and fallback configuration, doctor checks, hashed
  account-safe audit metadata, and capacity/fallback analytics with turn-duration context.
- Ensure source installs detect the new telemetry marker and reapply the maintained Codex patch
  instead of compiling a previously patched but pre-capacity checkout.

## 0.5.25 — 2026-09-19

- Install and roll back signed macOS release executables with atomic same-directory replacement,
  avoiding stale kernel code-signing state when a prior routed binary is overwritten in place.

## 0.5.24 — 2026-09-19

- Preserve the minimal macOS JIT entitlement on `codex-code-mode-host` across source installs,
  signed release artifacts, and routed Desktop builds so V8 can reserve its executable CodeRange.
- Fail builds and release installation closed unless a framed Code Mode session can execute real
  JavaScript, catching runtime-signing failures that `--help` and handshake-only checks miss.

## 0.5.23 — 2026-09-18

- Route spawned subagents from Codex's existing non-secret `task_name` while preserving the stock
  provider-facing collaboration schema and keeping the delegated task provider-encrypted; the
  internal ephemeral hint is excluded from rollout history and model-visible child input.
- Keep opaque follow-up turns on the exact child's prior tier and backend without leaking affinity
  between the root agent or sibling children.
- Attribute native flat `agent_id` / `agent_type` hook events to distinct subagent audit rows instead
  of collapsing them into the root session, while retaining compatibility with legacy nested fields.
- Install, trust, validate, and process Codex's `SubagentStop` hook so child completion, duration, and
  token usage come from the child transcript rather than the parent transcript.

## 0.5.22 — 2026-09-18

- Validate raw macOS CLI executables with strict Developer ID signature checks and Apple's accepted
  notarization result without passing them to `spctl`'s app-bundle assessor, which rejects valid
  standalone command-line tools with "does not seem to be an app."

## 0.5.21 — 2026-09-18

- Refuse to let `agentroute update` silently replace a newer source installation with an older
  published release; intentional rollback now requires `--allow-downgrade`.
- Start the routed CLI, exec, resume, app-server, and Desktop runtime on the configured NORMAL-tier
  backend before the first provider request, so an exhausted ChatGPT subscription cannot reject a
  turn before AgentRoute's prompt hook switches it to an API backend.
- Sign public macOS CLI binaries with the same Developer ID secret contract as Overwatchr, submit
  both native executables for Apple notarization, and require Gatekeeper assessment before release.

## 0.5.20 — 2026-09-18

- Add classifier health telemetry with explicit success, timeout, error, and fallback counts/rates,
  failure-reason breakdowns, and average/p50/p95 latency reporting; failed attempts no longer
  inherit stale token usage.
- Add a configurable model-capability registry for tool calling, reasoning, vision, context windows,
  and pricing identities, exposed through `agentroute models` and selection receipts.
- Reconcile routed turns as pending, completed-metered, completed-unmetered, failed, interrupted, or
  stale-unreconciled instead of treating every missing token receipt as the same condition.
- Replace the minimal prerequisite check with `agentroute doctor`, covering the routed runtime,
  hooks, provider configuration, credentials, classifier catalog, audit database, pricing, model
  capabilities, and routed Desktop installation.
- Make release updates transactional: validate the downloaded Codex executable before setup and
  restore the previous runtime files if execution or hook activation fails.
- Fail public macOS release builds closed unless their native binaries carry a Developer ID
  signature, preventing an ad-hoc-signed update from replacing a working local runtime.
- Add `agentroute backend-default BACKEND` as a credential-aware one-command way to move all
  intelligence tiers away from an exhausted subscription, with clearer prompt-vs-shell guidance.
- Deep-sign nested Codex executables on macOS during local, Desktop, and release builds so
  taskgated does not terminate them with `CODESIGNING / Taskgated Invalid Signature`.
## 0.5.19 — 2026-09-18

- Fix Stop-hook attribution to report the routed step model instead of the frozen session-start
  model, and normalize affected historical receipts while preserving the reported value for audit.
- Record first completion time and duration for every stopped turn, including turns without token
  receipts; add average, p50, p95, maximum, and longest-turn duration analytics.
- Correct historical DeepSeek and Azure model-cost attribution and visibly report normalized model
  mismatches rather than silently mixing backend and answer-model identities.

## 0.5.18 — 2026-09-18

- Add `agentroute analytics` for privacy-safe local model-usage reporting by backend, answer model,
  and UTC day/week/month buckets.
- Include observed answer tokens/costs, fixed-model savings estimates, and separate classifier
  token, latency, and cost breakdowns; support JSON and per-session reporting.

## 0.5.17 — 2026-09-18

- Make routing work in non-interactive `codex exec` sessions by registering the exact AgentRoute
  hooks in Codex's trust store during setup.
- Ask Codex's own app-server for canonical hook hashes and trust only matching AgentRoute commands;
  never bypass trust globally or authorize unrelated user/project hooks.
- Fail installation visibly when the routed runtime cannot discover or persist both required hooks.

## 0.5.16 — 2026-09-17

- Add checksum-verified prebuilt release installation and in-place updates.
- Add local Codex Desktop install, rebuild, status, and rollback commands without redistributing
  ChatGPT.app.
- Surface the selected route in Codex Desktop and preserve the compatible Codex app-server version.
- Add multi-platform release builds, provenance attestations, optional Apple Developer ID signing,
  and a manual release gate.
- Add weekly upstream Codex patch/compile checks with actionable compatibility issues.
- Add packaging, archive-safety, Desktop rollback, wheel-content, and patch-stack validation.
