# Changelog

## 0.5.51 — 2026-09-26

- Add `agentroute bridge serve`, a local Anthropic Messages bridge that exposes Claude models
  through the OpenAI Responses API, so Codex can route to them like any other backend. `agentroute
  bridge check` runs one live round trip through the selected credential.
- Translate Responses requests and streams in both directions: instructions to the system block,
  function and freeform `apply_patch` calls to `tool_use`, tool outputs to `tool_result`, base64
  images, usage totals, and the output-item SSE events Codex needs before text deltas. Duplicate
  and unsupported tool declarations are dropped instead of failing the turn upstream.
- Serve the Claude Code subscription credential from the macOS Keychain, persisting refreshed
  tokens so Claude Code stays logged in, or `ANTHROPIC_API_KEY` for the credential path Anthropic
  covers. Subscription use prints a warning and remains opt-in per backend.
- Negotiate Anthropic betas per model, including long-context beta for Sonnet and Opus classes and
  its omission for Haiku, which rejects it on subscriptions.

## 0.5.50 — 2026-09-25

- Improve related-session context precision with conversational stop-word filtering,
  Unicode-aware term matching, and weighted title, heading, and body evidence.
- Explain matched fields and terms in search previews; add a small judged prompt set
  covering relevant retrieval, unrelated-session rejection, and expected misses.
- Keep related-session context opt-in and make no token-efficiency claim pending
  longitudinal usage data.

## 0.5.49 — 2026-09-25

- Fix fresh source installs and release builds incorrectly rejecting a new clone
  as dirty before its first checkout. Existing dirty checkouts remain protected.
- Includes all 0.5.48 features below. The 0.5.48 packaging run failed before
  compilation and published no release assets; its tag is preserved unchanged.

## 0.5.48 — 2026-09-25

- Upgrade the routed CLI and matching Code Mode host to upstream Codex 0.157.0,
  with AgentRoute runtime v41. Preserve the real upstream version in `codex --version`.
- Build from an immutable `atiti/codex` revision containing 17 logically separated
  commits. Future ports use isolated worktrees, conflict-resolution reuse, and
  `range-diff` review; the installer refuses dirty source checkouts.
- Add bounded per-turn execution receipts and analytics for observed responses,
  cached tokens, tool completions/failures, tool work, and compaction. Partial
  measurements are labeled; overlapping tool work is not reported as model latency.
- Add opt-in cache economics (`agentroute context economics shadow|retain|off`),
  using recent same-model/provider evidence. Explicit route choices retain priority;
  subscription estimates do not trigger cash-cost retention decisions.
- Add opt-in related-session source references from existing Codex memory artifacts
  (`agentroute context search`, `agentroute context mode shadow|references|off`).
  Retrieval is bounded and scoped to the current project or an explicit allowlist.
  Both context retrieval and cache economics remain off by default.
- Desktop rebuilds still require a matching 0.157 release line. This release's live
  acceptance covers CLI GPT/Azure routing and tool continuation; Desktop/mobile
  interoperability has not been verified. Existing routed Desktop apps are not
  rebuilt by `agentroute update`.

## 0.5.47 — 2026-09-24

- Preserve the interrupted turn's effective model, backend, and reasoning effort for the next
  correction prompt. Explicit prompt tags override only their own setting; `@auto` clears the
  affinity. Capacity, account, and safety policies may still select a visible fallback.
- Record explicit Codex `Interrupt` hook outcomes in the local audit log, retain them through stop
  accounting, and add a regression-tested installer/doctor path for the hook.

## 0.5.46 — 2026-09-24

- Add opt-in, profile-aware Daybreak Blue model selection for ChatGPT subscription security
  turns. Request `gpt-daybreak-blue-latest` only when the selected account's fresh Codex catalog
  lists it; otherwise use the profile's ordinary GPT target. Keep the backend as `gpt` and show
  the selection or fallback in the route notice.

## 0.5.45 — 2026-09-23

- Record Codex's per-turn route-application receipt so the CLI audit distinguishes requested,
  applied, rejected, unavailable, and unknown routing outcomes, including the actual model,
  provider, reasoning effort, and rejection reason. Rejections appear as transcript commentary,
  not accumulated warnings.
- Attribute measured usage to the observed model/backend when Codex provides an application
  receipt; mark provider attribution unknown when the older CLI cannot prove it.
- Port the route-application receipt through the pinned Codex CLI hook protocol (runtime v40).

## 0.5.44 — 2026-09-23

- Show model-route notices as colored transcript items instead of warnings, so per-turn routing
  does not inflate the warning count; retain model, provider, and reasoning in the CLI status bar.

- Allow Desktop rebuilds when the official app and routed CLI share the same Codex
  `major.minor.patch` release line, even if prerelease suffixes differ; `desktop status` now
  reports release-line compatibility separately from exact version-string equality.

- Enforce a minimum `xhigh` reasoning effort whenever GPT-6 Luna is selected on GPT or Azure,
  including classifier-selected answer routes, inherited routes, and lower explicit-effort routes.
  The independent classifier call retains its configured low effort. Record the floor in the
  routing receipt and show the actual applied effort in the route banner.

- Port the complete routed Codex patch stack to upstream `rust-v0.156.1`
  (`b412ff32`, routing runtime v39), including its GPT-6 model catalog.
- Preserve same-provider Azure compaction checkpoints, including after resume,
  while still dropping encrypted state across provider/account boundaries.
  Normalize requests only at the shared client boundary.
- Use destination-specific context windows, compaction limits, modalities, and
  guidance when routing; retain admitted approval authority separately and never
  label fallback model metadata as verified.
- Preserve staged, unstaged, and untracked Codex source changes in a recoverable
  stash before upgrading the managed checkout to a new upstream revision.
- Recover missing custom-tool results as aborted during Codex history normalization, including
  debug builds, rather than panicking when reopening an interrupted thread.

## 0.5.43 — 2026-09-23

- Show a colorful `MODEL ROUTE` banner directly in the Codex transcript, making the selected tier,
  model, backend, and reasoning effort easy to scan without opening the warning panel.
- Port the complete routed Codex patch stack to upstream `rust-v0.156.1`, preserve same-provider
  Azure compaction state, and use destination-specific model metadata.
- Recover interrupted custom-tool history without panicking in debug or release builds.

## 0.5.42 — 2026-09-22

- Classify model tier and reasoning effort independently, persist the selected effort and its
  source, and preserve the strongest known effort across continuation turns.
- Reserve FAST for deterministic low-judgment work; floor communication, explanation, analysis,
  implementation, debugging, operations, and orchestration at NORMAL.
- Replace free-form classifier task names with a stable taxonomy suitable for calibration reports.
- Treat next-turn manual tier changes as nonjudgmental signals rather than automatically labeling
  the previous route as wrong, and expose explicit calibration labels and effort distributions.
- Reconcile superseded turns without inventing completion latency from user idle time.

## 0.5.41 — 2026-09-21

- Move AgentRoute's portable v2 subagent tools to the nonreserved
  `agentroute_collaboration` namespace, allowing Azure/OpenAI parents to delegate readable tasks
  to DeepSeek and other compatible providers without violating reserved collaboration schemas.
- Preserve the upstream reserved `collaboration` contract and encrypted delivery for explicit
  legacy/provider-managed calls; only AgentRoute's portable wrappers accept plaintext task
  messages.
- Deliver portable plaintext child instructions to constrained Responses-compatible providers as
  ordinary user input at the final request boundary, so DeepSeek actually receives the delegated
  task while encrypted/native inter-agent messages remain provider-local.
- Redact portable plaintext task arguments from dispatch traces and logs, and derive subagent
  usage hints from the provider's effective namespace so namespaced and non-namespaced providers
  show the command that will actually run.

## 0.5.40 — 2026-09-21

- Make the v2 collaboration `spawn_agent`, `send_message`, and `followup_task` message fields
  plaintext at the tool-schema boundary so Azure/OpenAI parents can delegate readable tasks to
  DeepSeek and other providers without producing provider-bound ciphertext.
- Preserve genuinely encrypted legacy and same-provider collaboration calls, while classifying
  only the three namespaced v2 collaboration tools as plaintext and keeping their arguments
  redacted from logs.
- Remove v0.5.39's ineffective post-routing ciphertext conversion and add exact task-envelope,
  plaintext metadata, and encrypted-compatibility regressions.

## 0.5.39 — 2026-09-21

- Convert provider-encrypted subagent tasks to the native plaintext inter-agent envelope after
  routing when a child switches providers, so Azure/OpenAI parents can delegate usable tasks to
  DeepSeek or other compatible backends without weakening same-provider encrypted delivery.
- Serialize routed Desktop builds with an exclusive lock so overlapping rebuilds fail clearly
  instead of racing during bundle replacement.
- Quarantine any destination bundle that appears during the final install step, restore the prior
  routed app, and fail without nesting or corrupting either application bundle.

## 0.5.38 — 2026-09-21

- Reapply provider tool compatibility after per-turn model resolution so provider-qualified
  cross-provider subagents use portable function tools plus `apply_patch` instead of exposing Code
  Mode's reserved custom `exec` schema.
- Add real new-turn and tool-router regressions covering Azure/OpenAI parents spawning
  DeepSeek-compatible children while preserving `exec_command`, `write_stdin`, and `apply_patch`.

## 0.5.37 — 2026-09-21

- Restore Azure and OpenAI compatibility for the reserved `collaboration.spawn_agent` tool by
  keeping its model-visible v2 schema identical to upstream Codex.
- Remove the noncanonical child `backend` argument while preserving provider inheritance and
  cross-provider child routing through the canonical `model` field with a qualified model value.
- Preserve prompt-level and child-level reasoning-effort overrides without changing the reserved
  collaboration tool contract.

## 0.5.36 — 2026-09-21

- Complete subagent provider inheritance with typed, ephemeral routing metadata carried from the
  Codex spawn boundary into the child's first `UserPromptSubmit` hook. Children now inherit the
  parent's effective provider without inferring it from an already rewritten model alias.
- Add an optional `backend` field to Codex's v2 `spawn_agent` tool so one child can explicitly use
  another configured provider while the parent and siblings keep their own routes. The selected
  backend becomes affinity for that child's follow-up turns only.
- Fail closed with actionable messages when a requested child backend is unavailable or an
  explicitly requested child model is not configured for that backend. Spawn-only metadata stays
  out of serialized transcripts, generated public API schemas, and model-visible history.
- Add prompt-level reasoning effort overrides. Prefix `@ultra` (or another supported effort) with
  any provider and tier, such as `@azure @ultra`, to retain the selected route while explicitly
  setting that turn's request effort. The override is visible in the route notice and audit receipt.

## 0.5.35 — 2026-09-21

- Preserve the parent turn's effective provider on a subagent's first routed turn, while keeping
  explicit child backend overrides and child-specific affinity isolated from the parent and siblings.
- Let an explicit subagent model select another provider when that model maps to exactly one
  configured backend; ambiguous model names retain the inherited provider.

## 0.5.34 — 2026-09-21

- Replace isolated per-subscription `CODEX_HOME` trees with one canonical Codex home and
  turn-bound ChatGPT account routing. Configuration, hooks, MCPs, skills, plugins, sessions,
  history, and rules now stay together in the running Codex home.
- Preserve `~/.codex/auth.json` as the implicit backward-compatible `default` account. New named
  accounts store credentials only under `~/.codex/accounts/<name>/auth.json` and are selected by
  AgentRoute at `UserPromptSubmit` without restarting or changing `CODEX_HOME`.
- Add `agentroute account` commands to add, sign in to, migrate, select, inspect, prioritize, and
  remove ChatGPT accounts. Migration copies only `auth.json` from a legacy isolated home, leaving
  its configuration and session history untouched.
- Keep legacy `capacity profile-*` configuration readable during upgrade, while routing receipts,
  capacity status, doctor guidance, and user-visible notices use the clearer account terminology.

## 0.5.33 — 2026-09-21

- Add `agentroute backend-add` for arbitrary OpenAI Responses-compatible providers,
  including credential-free local Ollama endpoints.
- Support dynamic `@backend-name` session routing, optional per-tier custom model maps,
  and generated provider configuration in the active `CODEX_HOME` profile.
- Default custom providers to the constrained `functions_and_apply_patch` tool mode;
  operators must explicitly opt into full tool compatibility after validating an endpoint.
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
