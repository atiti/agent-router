# AgentRoute

AgentRoute is a local, deterministic model router for coding agents. It selects a model and
reasoning effort for every user turn while keeping the same Codex thread, transcript, tools, and
working context.

It is deliberately boring infrastructure: rules are inspectable, decisions are auditable, prompt
text stays local, and `@fast`, `@normal`, `@smart`, or `@max` always gives the human control.

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

The native patch applies the chosen settings before the first model call of that turn. A short
confirmation such as `go ahead` inherits the previous task's tier within that session.

| Tier | Default Codex target | Typical work |
|---|---|---|
| FAST | `gpt-5.6-luna`, low | renames, formatting, mechanical edits |
| NORMAL | `gpt-5.6-terra`, medium | routine implementation |
| SMART | `gpt-5.6-sol`, high | debugging, security, complex changes |
| MAX | `gpt-6-astra`, high | architecture and high-risk cross-cutting work |

All mappings, thresholds, and risk floors are editable in `~/.agentroute/config.yaml`.

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
```

Use `agentroute observe` to audit decisions without changing models and `agentroute enable` to
resume native switching.

When a route is applied, Codex prints a highlighted line before the response, for example:

```text
◆ MODEL ROUTE · using gpt-5.6-sol · high reasoning for this turn
```

The status bar also reflects the active model and effort. Code Mode remains enabled by installing
the companion host already distributed with stock Codex. Set `AGENTROUTE_CODE_MODE_HOST` if your
Codex package keeps it in a non-standard location.

## Privacy and failure behavior

- Routing is local and makes no network request.
- SQLite stores a SHA-256 prompt hash, not prompt text, by default.
- Hook failures fail open so a router error does not block Codex.
- Existing Codex hooks are preserved during installation.
- Config can cap the highest tier and set mandatory floors for risky work.

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
