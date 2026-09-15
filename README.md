# AgentRoute

AgentRoute is a local-first, auditable model router for coding agents. It selects a model and
reasoning effort for every user turn while keeping the same Codex thread, transcript, tools, and
working context.

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
    │                           model + reasoning effort
    ▼                                  │
the same active Codex thread ◄─────────┘
```

The native patch applies the chosen settings before the first model call of that turn. Explicit
read-only retrievals, mechanical edits, and simple questions about existing context take a
deterministic high-confidence FAST lane; hard risk floors still win. Ambiguous decisions can be
sent to a small OpenAI-compatible classifier with a two-second timeout and immediate heuristic
fallback. A short
confirmation such as `ok do it` is never scored as a new tiny task: AgentRoute reads the previous
assistant final answer from Codex's local transcript and classifies that task definition. If the
transcript is unavailable, it inherits the previous selected tier.

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

The key is never written to AgentRoute configuration or audit storage. To keep all classification
local, point the same interface at an OpenAI-compatible loopback endpoint such as Ollama; local HTTP
is permitted, while remote endpoints must use HTTPS:

```sh
agentroute classifier-enable \
  --endpoint http://127.0.0.1:11434/v1/chat/completions \
  --model qwen3:4b
```

Use `agentroute classifier-disable` to return to deterministic-only routing.

## Install locally

Requirements: macOS or Linux, Git, `uv`, Rust/Cargo, a working Codex login, and at least 6 GiB of
free disk space for the Codex build.

```sh
git clone https://github.com/YOUR_ORG/agentroute.git
cd agentroute
./scripts/install.sh
source ~/.zshrc
agentroute doctor
codex
```

The installer places everything under `~/.agentroute/`, adds `~/.agentroute/bin` to PATH, merges a
hook into `~/.codex/hooks.json` after making a backup, and exposes:

- `codex`: patched Codex with native step model switching and Code Mode enabled
- `codex-code-mode-host`: the Code Mode host shipped with the stock Codex package, installed beside
  the patched binary after an executable compatibility check
- `codex-stock`: the Codex binary that was active before installation
- `agentroute`: configuration, simulation, audit, and diagnostics CLI

It never replaces the original Codex binary. Set `AGENTROUTE_HOME` to choose another installation
root. `AGENTROUTE_CONFIG` and `AGENTROUTE_DATA_DIR` can override individual state paths.
The default local build uses Codex's stripped `dev-small` profile to limit disk use; set
`AGENTROUTE_BUILD_PROFILE=release` if you prefer an optimized build and have ample free space.

## Try it

```sh
agentroute test "Rename the account label"
agentroute test "Redesign authentication for a zero-downtime migration"
agentroute test "@max audit this concurrency design"
agentroute why
agentroute history
agentroute audit-report
agentroute label 42 correct --notes "appropriate model for the completed task"
```

Use `agentroute observe` to audit decisions without changing models and `agentroute enable` to
resume native switching.

When a route is applied, Codex prints a highlighted line before the response, for example:

```text
◆ MODEL ROUTE · SMART → gpt-5.6-sol · high reasoning · classifier confidence 84% · rule score 2.5 · implementation
```

The status bar also reflects the active model and effort. Code Mode remains enabled by installing
the companion host already distributed with stock Codex. Set `AGENTROUTE_CODE_MODE_HOST` if your
Codex package keeps it in a non-standard location.

The pinned Codex build rejects automatic transitions to `gpt-6-astra` when its Node REPL safety
metadata differs from the model admitted at turn start. AgentRoute therefore reports and applies a
MAX→SMART safety fallback for automatic routes instead of claiming a rejected switch. An explicit
`@max` remains the operator escape hatch and may be rejected by Codex when that incompatibility is
present.

## Privacy and failure behavior

- Deterministic routing is local. The optional classifier makes network requests only after
  `classifier-enable --allow-remote`; loopback endpoints do not require that flag.
- SQLite stores a SHA-256 prompt hash, not prompt text, by default.
- Continuation routing reads only a bounded tail of Codex's local transcript; task text is not
  copied into the AgentRoute audit database.
- Credential-shaped values trigger a visible warning and a SMART minimum route.
- Hook failures fail open so a router error does not block Codex.
- Existing Codex hooks are preserved during installation.
- Config can cap the highest tier and set mandatory floors for risky work.

For deterministic routes, the displayed percentage is explicitly **rule confidence**, not a
statistically calibrated probability. For LLM-classified routes, it is the classifier's stated
confidence and remains uncalibrated until enough labeled local outcomes exist. The separate rule
score always remains visible. Audit rows include the classification source, classifier task type,
hashed classifier reason, proposed and final tiers, comparison tier, task-context usage, and
risk-floor application. Use `agentroute label` to build a local calibration set. Approved agent
requests are also recorded. Manual overrides automatically label the previous automatic decision
as overridden.

## Native Codex patch

The patch extends synchronous `UserPromptSubmit` hook output with optional `model` and
`reasoningEffort` fields, then applies them through Codex's existing turn-settings machinery before
the first step begins. Async hooks cannot change execution settings.

The installer pins OpenAI Codex commit `b0af519c39766c173191fc39b341808619b51c74`. The maintained
patch is in `patches/codex-user-prompt-model-override.patch`. AgentRoute is not affiliated with or
endorsed by OpenAI.

## Development

```sh
uv sync --all-extras
uv run pytest
uv run ruff check .
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Run `./scripts/uninstall.sh` for safe removal instructions.
Licensed under Apache-2.0.
