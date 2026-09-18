# Changelog

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
