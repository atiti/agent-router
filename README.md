# AgentRoute

AgentRoute is a local-first, auditable model router for coding agents. It selects a model,
reasoning effort, and execution backend for every user turn while keeping the same Codex thread,
transcript, tools, and working context.

It is deliberately boring infrastructure: rules are inspectable, decisions are auditable, and
`@fast`, `@normal`, `@smart`, or `@max` always gives the human control. An optional LLM classifier
can resolve ambiguous turns; it is disabled until explicitly configured.

> **Alpha:** Codex does not currently accept model overrides from `UserPromptSubmit` hooks. The
> installer builds a narrowly patched Codex from the pinned upstream commit documented below.
> This makes routing native to the active session instead of launching a new process per turn.

## How one turn is routed

```text
user prompt
    │
    ▼
Codex UserPromptSubmit hook ──► AgentRoute classifier ──► SQLite audit
    │                                  │
    │                    provider + model + reasoning effort
    ▼                                  │
the same active Codex thread ◄─────────┘
    │
    └── Stop hook ──► exact per-turn token counters ──► cost statistics
```

The native patch applies the chosen settings before the first model call of that turn. Explicit
read-only retrievals, mechanical edits, bounded replies/messages, and simple questions about existing context take a
deterministic high-confidence FAST lane; hard risk floors still win. Ambiguous decisions can be
sent to a small OpenAI-compatible classifier with a two-second timeout and immediate heuristic
fallback. A short
confirmation such as `ok do it` is never scored as a new tiny task: AgentRoute reads the previous
assistant final answer from Codex's local transcript and classifies that task definition. If the
transcript is unavailable, it inherits the previous selected tier.

Spawned subagents are routed independently. Their triggering task is classified by the same hook
before the child's first model call, and each audit row and route banner identifies whether the
decision belongs to the root agent or a subagent. A child starts with the parent's full or bounded
context according to Codex's spawn request, but it does not have to keep the parent's model or
backend.

The classifier is a second-stage judge, not the primary router. Explicit overrides, approved agent
requests, high-confidence rules, and credential-shaped prompts never reach it. It returns the
cheapest sufficient tier, confidence, and task type; only the task type, confidence, and a hash of
its explanation are audited.

An agent that knows its current tier is insufficient can explicitly ask for the next turn's tier by
ending its final response with two visible plain-text lines:

```text
MODEL_REQUEST: SMART
MODEL_REQUEST_REASON: The production failure spans several services.
```

AgentRoute accepts the request only when the next user turn is an explicit confirmation such as
`ok`, `ok do it`, or `proceed`. Policy caps, risk floors, and Codex model-compatibility checks still
apply. The requested tier and a SHA-256 hash of the reason are audited; the reason text is not.

| Tier | Default Codex target | Typical work |
|---|---|---|
| FAST | `gpt-5.6-luna`, low | renames, formatting, mechanical edits |
| NORMAL | `gpt-5.6-terra`, medium | routine implementation |
| SMART | `gpt-5.6-sol`, high | debugging, security, complex changes |
| MAX | `gpt-6-astra`, high | architecture and high-risk cross-cutting work |

All mappings, thresholds, and risk floors are editable in `~/.agentroute/config.yaml`.

## Execution backends

The built-in `gpt` backend uses the existing ChatGPT subscription login. Azure OpenAI and
DeepSeek use API credentials from environment variables; AgentRoute never writes those secrets to
configuration or audit storage. Enable and map them with:

```sh
# Azure's URL includes /openai/v1; both Azure hostname forms are supported.
export AZURE_OPENAI_API_KEY="..."
agentroute backend-enable azure \
  --base-url https://YOUR-RESOURCE.openai.azure.com/openai/v1 \
  --fast-model YOUR_FAST_DEPLOYMENT \
  --normal-model YOUR_NORMAL_DEPLOYMENT \
  --smart-model YOUR_SMART_DEPLOYMENT \
  --max-model YOUR_MAX_DEPLOYMENT

# Some resources expose the equivalent hostname:
# https://YOUR-RESOURCE.cognitiveservices.azure.com/openai/v1

export DEEPSEEK_API_KEY="..."
agentroute backend-enable deepseek

agentroute backend-route fast deepseek
agentroute backend-route normal azure
agentroute backend-status
```

For an OpenAI-compatible bearer-auth proxy in front of Azure, use its `/v1` base URL and add
`--api-key-header authorization`.

For a persistent local test without putting a key in YAML or Codex TOML, import it from the
current process into AgentRoute's owner-only credential file, then unset the source variable:

```sh
agentroute backend-credential-import deepseek DEEPSEEK_API_KEY
unset DEEPSEEK_API_KEY
```

Backend mappings are defaults, not lock-in. Prefix a prompt with `@gpt`, `@azure`, or
`@deepseek`; combine it with a tier override in either order, such as
`@deepseek @smart review this design`. Disabled or unavailable configured backends visibly fall
back to `gpt`. An explicit backend becomes the root session preference, so contextual follow-ups
stay on the same backend and selected tier by default; use another backend prefix to switch or
`@auto` to return to automatic backend selection. Subagents keep independent preferences. Routing
prefixes are removed before the task is recorded or sent to the model.

Provider changes happen inside the active thread: Codex rebuilds only its
provider-specific request session while retaining the local conversation, tools, and turn state.
Once a thread has mixed providers, AgentRoute removes provider-bound
encrypted reasoning, compaction state, encrypted function arguments, and response item IDs while
preserving ordinary messages and portable tool-call history on every later provider request.
AgentRoute also applies a provider-declared tool compatibility profile. DeepSeek currently receives
ordinary function tools plus the `apply_patch` custom tool because its Responses API rejects
Codex's custom Code Mode `exec` tool. This changes only the wire format: the active Codex sandbox,
approval, Guardian, reviewer, and Node/Code Mode safety authority remains local and unchanged.
The same compatibility profile is applied when Codex starts an isolated automatic-review session,
so its existing read-only inspection tools are encoded as ordinary functions rather than an
unsupported custom `exec` tool.

Automatic approval review is routed independently from the worker tier. Each API backend defaults
to its FAST model at low reasoning effort, so a SMART or MAX worker does not make a bounded command
review unnecessarily expensive. The review stays on the same backend and credential boundary for
predictable availability and tool compatibility. Pin a separately validated reviewer when enabling
a backend with `--review-model MODEL`; `agentroute backend-status` shows the effective reviewer.
Guardian remains fail-closed and retains the same local policy, sandbox evidence, and authority
regardless of reviewer model.

## Optional LLM classifier

For best routing quality, use a small cloud model only for ambiguous turns. This avoids maintaining
a local model runtime while deterministic turns remain sub-millisecond. AgentRoute requires a
dedicated environment variable and explicit permission before any bounded task context leaves the
machine:

```sh
export AGENTROUTE_CLASSIFIER_API_KEY="..."
agentroute classifier-enable --allow-remote \
  --endpoint https://api.openai.com/v1/chat/completions \
  --model gpt-5-mini
agentroute classifier-status
```

The key is never written to AgentRoute configuration or audit storage. It can also be read from an
owner-only file, which avoids requiring a long-lived shell environment variable. Verify the model
against the provider's `/v1/models` catalog after enabling it:

```sh
install -m 600 /private/path/classifier.key ~/.agentroute/classifier.key
agentroute classifier-enable --allow-remote \
  --endpoint https://models.example.com/v1/chat/completions \
  --model classifier-model \
  --api-key-file ~/.agentroute/classifier.key
agentroute classifier-verify
```

For a LiteLLM service exposed only on a private or Tailscale IP, HTTP requires a second, narrowly
scoped acknowledgement. Public HTTP endpoints remain rejected:

```sh
agentroute classifier-enable --allow-remote --allow-private-http \
  --endpoint http://100.64.0.10:4000/v1/chat/completions \
  --model dev-classifier \
  --api-key-file ~/.agentroute/classifier.key \
  --timeout 5 --reasoning-effort low
agentroute classifier-verify
```

To keep all classification local, point the same interface at an OpenAI-compatible loopback
endpoint such as Ollama; loopback HTTP is permitted without remote-egress flags:

```sh
agentroute classifier-enable \
  --endpoint http://127.0.0.1:11434/v1/chat/completions \
  --model qwen3:4b
```

Use `agentroute classifier-disable` to return to deterministic-only routing.

## Install

The recommended installer downloads the release for the current OS and CPU, verifies its SHA-256
checksum, and installs it under `~/.agentroute/`. It never replaces the official Codex binary.
`uv` and a working Codex login are the only prerequisites.

```sh
curl -fsSL https://raw.githubusercontent.com/atiti/agent-router/main/scripts/bootstrap.sh | sh
source ~/.zshrc  # use ~/.profile on non-zsh shells
agentroute doctor
codex
```

For a review-before-running flow, download `scripts/bootstrap.sh`, inspect it, and execute it with
`sh`. Every GitHub release includes per-platform checksums and GitHub build-provenance attestations.
macOS release binaries are Developer ID signed when the project signing secrets are configured;
the installer always verifies the published checksum before executing the bundled installer.
Downloaded archives can also be independently checked with
`gh attestation verify ARCHIVE --repo atiti/agent-router`.

To update later:

```sh
agentroute update
```

### Build from source

Source builds require macOS or Linux, Git, Node/npm, `uv`, Rust/Cargo, a working Codex login, and
at least 6 GiB of free disk space.

```sh
git clone https://github.com/atiti/agent-router.git
cd agentroute
./scripts/install.sh
source ~/.zshrc
agentroute doctor
codex
```

Both installers place everything under `~/.agentroute/`, add `~/.agentroute/bin` to PATH, merge a
hook into `~/.codex/hooks.json` after making a backup, and expose:

- `codex`: patched Codex with native step model switching and Code Mode enabled
- `codex-code-mode-host`: a pinned official Codex package host from the same release lineage as the
  patched CLI, selected for wire-protocol compatibility instead of copying an arbitrary stock host
- `codex-stock`: the Codex binary that was active before installation
- `agentroute`: configuration, simulation, audit, and diagnostics CLI

It never replaces the original Codex binary. Set `AGENTROUTE_HOME` to choose another installation
root. `AGENTROUTE_CONFIG` and `AGENTROUTE_DATA_DIR` can override individual state paths.
During setup, AgentRoute asks the installed Codex app-server to calculate canonical hashes for the
two exact AgentRoute hook commands and records trust only for those entries. This makes the same
routing lifecycle work in the interactive TUI, Desktop, and headless `codex exec`; no global hook
trust bypass is enabled, and unrelated hooks retain their existing trust state.
The default local build uses Codex's stripped `dev-small` profile to limit disk use; set
`AGENTROUTE_BUILD_PROFILE=release` if you prefer an optimized build and have ample free space.
The installer pins the official Code Mode host package alongside the Codex source commit. Advanced
installations can provide a preverified executable with `AGENTROUTE_CODE_MODE_HOST`.

## Try it

```sh
agentroute test "Rename the account label"
agentroute test "Redesign authentication for a zero-downtime migration"
agentroute test "@deepseek @smart audit this concurrency design"
codex exec '@azure summarize the latest test result'
agentroute why
agentroute history
agentroute audit-report
agentroute stats --baseline gpt-6-astra
agentroute label 42 correct --notes "appropriate model for the completed task"
```

Use `agentroute observe` to audit decisions without changing models and `agentroute enable` to
resume native switching.

`agentroute stats` uses Codex's cumulative per-turn transcript counters for input, cached input,
cache-write input, output, and reasoning-output tokens. It estimates routed answer cost, the cost
of running the same observed token counts on a fixed baseline model, classifier overhead, and net
savings. Reasoning tokens are already included in output tokens and are not charged twice. Prices
and aliases are editable under `pricing` in `~/.agentroute/config.yaml`; the bundled defaults were
checked on 2026-09-17 against the official OpenAI and DeepSeek model pages. DeepSeek's bundled Flash
rate uses its conservative peak price; actual off-peak charges are lower. Azure deployment prices
vary, so add their rates or aliases explicitly; unpriced models are named and excluded instead of
silently presented as free. This is an API-equivalent estimate, not a Codex subscription invoice,
and a different model may produce a different number of tokens.

For a local model mix and timeline, use:

```sh
agentroute analytics --days 30 --bucket day
# Longer rollup:
agentroute analytics --all --bucket month
# Private, scriptable output (contains no prompt text):
agentroute analytics --days 7 --json
```

`analytics` groups answer usage by backend and answer model, then repeats that breakdown over UTC
day/week/month buckets. It also reports classifier calls, token usage, latency, and estimated
overhead separately. `--session` limits the report to a single local Codex session. Like `stats`,
it uses local audit metadata and observed token counters only; no prompts, tool arguments, or
response text are emitted.

When a route is applied, Codex prints a highlighted line before the response, for example:

```text
◆ MODEL ROUTE · SMART → deepseek-flash · high reasoning · backend deepseek/agentroute-deepseek · scope subagent · source LLM/private · classifier confidence 84% · rule score 2.5 · implementation
```

DeepSeek documents `deepseek-flash` as the API model ID for the current DeepSeek-V4.1-Flash release.
AgentRoute uses it for every DeepSeek intelligence tier and varies reasoning effort by tier. The
status bar also reflects the active model and effort. Code Mode remains enabled by installing
the companion host already distributed with stock Codex. Set `AGENTROUTE_CODE_MODE_HOST` if your
Codex package keeps it in a non-standard location.

The pinned Codex build rejects automatic transitions to `gpt-6-astra` when its Node REPL safety
metadata differs from the model admitted at turn start. AgentRoute therefore reports and applies a
MAX→SMART safety fallback for automatic routes instead of claiming a rejected switch. An explicit
`@max` remains the operator escape hatch and may be rejected by Codex when that incompatibility is
present.

## Codex Desktop on macOS

AgentRoute can derive a separate routed copy from the official app already installed on the Mac:

```sh
agentroute desktop install
open -a /Applications/ChatGPT-Routed.app
```

The builder does not modify the original app. It embeds the AgentRoute Codex binary, its matching
Code Mode host, and a launcher that loads the owner-only backend credential file. It retains the
original bundle identifier for frontend compatibility, changes the visible name, disables automatic
updates, signs nested code inside the copied bundle, verifies the complete signature, and smoke-tests
the embedded app-server before publishing the destination. The official app is never uploaded,
packaged, or redistributed.
The installer also refuses a stock/routed Codex version mismatch by default, because that can make
mobile remote clients report that the Desktop app must be updated. `agentroute desktop status`
shows all three versions before any change is made.

After updating the official ChatGPT app or AgentRoute, rebuild with an automatic rollback copy:

```sh
agentroute desktop rebuild
agentroute desktop status
# If needed:
agentroute desktop rollback
```

The default `-` identity is an ad-hoc signature for local testing. To use an installed Apple signing
identity instead, pass `--signing-identity 'Developer ID Application: …'`. The copied app
cannot retain OpenAI-only application groups, push, or Keychain access groups under another identity,
so those restricted entitlements are deliberately omitted. Close the stock app before normal use of
the routed copy: both retain `com.openai.codex` and therefore share the normal Codex profile and
single-instance identity. App updates do not update the routed copy; rebuild it from the new stock app.

## Privacy and failure behavior

- Deterministic routing is local. The optional classifier makes inference requests only after
  `classifier-enable --allow-remote`; loopback endpoints do not require that flag. Non-loopback
  HTTP is accepted only for literal private/Tailscale IPs with `--allow-private-http`.
- Remote credentials can come from an environment variable or a mode-600 file. Provider catalog
  verification is cached in configuration; turns do not add a catalog request to the hot path.
  An unverified or stale remote catalog fails back to deterministic routing until
  `agentroute classifier-verify` refreshes it. The installed Codex launcher runs a no-op-fast
  `classifier-refresh` at session start and contacts the catalog only when the receipt is missing
  or stale.
- SQLite stores a SHA-256 prompt hash, not prompt text, by default.
- Continuation routing reads only a bounded tail of Codex's local transcript; task text is not
  copied into the AgentRoute audit database.
- Credential-shaped values trigger a visible warning and a SMART minimum route.
- Hook failures fail open so a router error does not block Codex.
- Existing Codex hooks are preserved during installation.
- A fail-open Stop hook records exact answer token counters against the matching session and turn.
- Config can cap the highest tier and set mandatory floors for risky work.

For deterministic routes, the displayed percentage is explicitly **rule confidence**, not a
statistically calibrated probability. For LLM-classified routes, it is the classifier's stated
confidence and remains uncalibrated until enough labeled local outcomes exist. The separate rule
score always remains visible. Audit rows include the classification source, classifier task type,
hashed classifier reason, proposed and final tiers, comparison tier, task-context usage, and
risk-floor application. Each row also contains a hashed selection receipt with the resolved-task
hash, candidate model/effort pairs, explicit exclusions, policy state, cached catalog receipt, and
provider-returned model metadata. LLM rows distinguish previous context sent from explicit task
inheritance, hash the exact request body, and record request latency plus numeric token-usage fields
returned by the provider. Prompt and classifier reason text remain excluded. Use `agentroute label`
to build a local calibration set. Approved agent requests are also recorded. Manual overrides
automatically label the previous automatic decision as overridden.

## Native Codex patch

The patch extends synchronous `UserPromptSubmit` hook output with optional `model`,
`modelProvider`, `reasoningEffort`, prompt-prefix stripping, and mixed-provider-state fields, then
applies them through Codex's existing
turn-settings machinery before the first step begins. Triggering subagent communications pass
through the same hook. A provider switch creates a fresh provider-specific client session so
websocket, authentication fallback, and sticky-routing state cannot leak across backends. Every
provider-owned history item is tagged with its producer provider. At a provider boundary, Codex
removes only opaque reasoning/compaction state produced by another provider; portable messages and
tool results remain, while reasoning produced by the active provider survives later tool-call
continuations and turns. Legacy untagged opaque state is removed on the first mixed-provider turn.
Routing and provider-state filtering also happen before automatic pre-turn compaction, because
compaction is itself a provider request and can otherwise fail before the ordinary turn begins.
Local compaction reuses that routed request session instead of silently constructing an unfiltered
default-provider session. When the turn crosses a provider boundary, Codex also skips the
previous-model compaction pass: that model belongs to the old backend and cannot safely be sent
through the newly selected provider.
This boundary is inferred from transcript provenance as well as hook audit state, so resumed and
forked sessions remain safe even when they have a new audit-session identifier.
The local compactor consumes the immutable routed step settings, including its model, reasoning,
service-tier, and telemetry selection; it never falls back to stale turn-start model metadata.
For function-compatible providers, automatic approval reviews retain the same read-only Guardian
authority but explicitly disable Code Mode so reviewer tools are serialized as ordinary functions.
Async hooks cannot change execution settings.

For a load-balanced Responses API backend, the proxy must also keep encrypted reasoning on the
deployment that created it. LiteLLM supports this with:

```yaml
router_settings:
  enable_pre_call_checks: true
  optional_pre_call_checks:
    - encrypted_content_affinity
```

AgentRoute deliberately leaves same-provider encrypted item identifiers intact so that affinity
can work. This preserves reasoning continuity without attempting to decrypt or copy provider-private
state across trust boundaries.

The installer pins OpenAI Codex commit `b0af519c39766c173191fc39b341808619b51c74`. The maintained
patches are `patches/codex-user-prompt-model-override.patch`,
`patches/codex-desktop-route-notice.patch`, and `patches/codex-package-version.patch`.
A weekly GitHub Actions job applies all patches to the latest upstream Codex release and compiles
the CLI. Failures open one actionable compatibility issue; automation never publishes an unreviewed
Codex upgrade. AgentRoute is not affiliated with or endorsed by OpenAI.

## Development

```sh
uv sync --all-extras
uv run pytest
uv run ruff check .
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Run `./scripts/uninstall.sh` for safe removal instructions.
Licensed under Apache-2.0.
