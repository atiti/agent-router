# Changelog

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
