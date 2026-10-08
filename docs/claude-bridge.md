# Running Claude models through the bridge

Claude models are not served over the OpenAI Responses API, so AgentRoute ships a small local
bridge. It accepts Responses requests from Codex, calls the Anthropic Messages API, and streams
the answer back in the shape Codex expects. Codex keeps its sandbox, approvals, and tool
authority; the bridge only translates.

This guide is the whole setup path. Everything below is local to your machine.

## Quick start

```sh
agentroute bridge install     # register the backend, install the service, wait until healthy
agentroute bridge check       # one live round trip through the credential
codex
```

Then prefix a prompt with `@claude`:

```
@claude explain how the retry logic in this file works
```

`agentroute bridge install` does four things: registers a `claude` backend with the standard
tiers, writes it into Codex's provider block, installs a per-user background service, and waits
until the bridge answers on loopback. It is safe to re-run, and an existing `claude` backend
keeps its own tier choices.

## Choosing a credential

| Credential | Flag | Billing |
|---|---|---|
| Claude Code subscription | `--credential claude-code` (default) | Your Claude plan's usage limits |
| Anthropic Console API key | `--credential api-key` | Per-token API billing |

The subscription path reads the same rotating token Claude Code stores in the macOS Keychain and
writes refreshed tokens back, so Claude Code keeps working. The bridge also replays Claude Code's
identity block, which Anthropic requires before it serves a subscription credential.

Bridging a subscription sits outside Anthropic's terms of service for third-party clients. The
Console API key is the path those terms cover, and it is what to use for a supported setup:

```sh
export ANTHROPIC_API_KEY=sk-ant-...
agentroute bridge install --credential api-key
```

To store the key once instead of exporting it in every shell, use
`agentroute backend-credential-import claude ANTHROPIC_API_KEY`.

## Checking that it works

```sh
agentroute bridge status      # installed, loaded by the supervisor, and answering?
agentroute bridge check       # one live Claude round trip
agentroute bridge usage       # current subscription limits, if you use a subscription
agentroute doctor             # includes a `bridge` row
```

`agentroute doctor` reports the bridge alongside the rest of the install. The row PASSes when the
service is installed and answering on loopback, and FAILs with the exact next command otherwise.
It is skipped when no Claude backend is configured, so an install that never opted in does not
collect a permanent warning.

## Subscription usage limits

`agentroute bridge usage` prints the live 5-hour session, the weekly limit across all models, and
any model-scoped weekly cap, read from Anthropic's OAuth usage endpoint:

```
┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┓
┃ limit               ┃ used ┃ left ┃ resets                  ┃ active ┃
┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━┩
│ 5h session          │  42% │  58% │ 2026-09-27 11:30 CEST │  yes   │
│ weekly (all models) │   9% │  91% │ 2026-10-02 14:00 CEST │        │
│ weekly (Fable)      │   0% │ 100% │ 2026-10-02 14:00 CEST │        │
└─────────────────────┴──────┴──────┴─────────────────────────┴────────┘
```

`--offline` prints only the last recorded sample. The same two windows show up inside Codex: the
bridge translates Anthropic's rate-limit headers into Codex's own limit family, so `/status`
renders them as 5h and Weekly rows while a Claude turn is active.

The `◆ MODEL ROUTE` notice also shows the recorded 5h and weekly percentages for a bridge
installed with `--credential claude-code`, including when the sample was recorded. It replaces
the API-equivalent dollar estimate on that route. It does not fetch the account endpoint during
every prompt; run `agentroute bridge usage` when you need the current account value. An API-key
bridge keeps its ordinary budget notice.

The bridge renews an expired subscription token through Claude Code's own refresh flow. If the
endpoint refuses, the error names the case: a dead refresh token asks you to run `claude` and
`/login`, a Cloudflare block names the cause, and a throttle says to retry.
`agentroute bridge refresh` forces a renewal on demand.

## Managing the service

```sh
agentroute bridge install            # idempotent; re-run after changing the port
agentroute bridge install --port 8091
agentroute bridge status
agentroute bridge uninstall
```

The service is a LaunchAgent at `~/Library/LaunchAgents/com.agentroute.claude-bridge.plist` on
macOS, or a `systemd --user` unit at `~/.config/systemd/user/agentroute-claude-bridge.service` on
Linux. It restarts automatically and logs to `~/.agentroute/bridge.log`.

To debug, run the same bridge in the foreground instead:

```sh
agentroute bridge serve --port 8090
```

## Routing Claude for some or all turns

Installing the bridge registers Claude but does not change your defaults. Choose what you want:

```sh
@claude ...                            # one turn
agentroute backend-route normal claude # one tier
agentroute backend-default claude      # every tier, for example when a plan is exhausted
agentroute backend-route normal gpt    # back to the subscription
```

Tier overrides compose, so `@claude @smart ...` runs that turn on `claude-sonnet-5-5` at high effort, and the next
prompt can switch back to `@gpt`.

### Switching after a Claude usage limit

When Claude reaches its 5-hour or weekly limit, use a different backend in the same session:

```text
@gpt continue
@azure-direct @max continue
```

Claude's recorded limit belongs to Claude and does not count against your ChatGPT quota.
AgentRoute keeps a genuine ChatGPT account lock and API budget limits in force, and a fresh
healthy account read can recover a stale session lock. Check `agentroute capacity status`
for the destination account's current capacity.

`@auto continue` clears your manual route and returns to configured routing. It can use another
backend only when a fallback is configured; it does not search every enabled provider. To permit
GPT routes to fall back to your existing Azure Direct backend, configure:

```sh
agentroute capacity fallback gpt azure-direct
```

An explicit provider remains selected until another provider tag or `@auto` clears it. A tier
such as `@max` chooses the model within that provider; it does not select a different provider.

## Models, tiers, and pricing

The bridge serves the tiers of the `claude` backend and advertises them through
`model_catalog_url`, so Codex's model picker shows real Claude slugs instead of "unknown model".

| Tier | Model | Input | Cached input | Cache write (5m) | Output |
|---|---|---|---|---|---|
| FAST (low effort) | `claude-haiku-5-5` | $0.10 / $0.50 | $0.01 / $0.05 | $0.125 / $0.625 | $0.50 / $2.50 |
| NORMAL (medium effort) | `claude-sonnet-5-5` | $2 | $0.20 | $2.50 | $10 |
| SMART (high effort) | `claude-sonnet-5-5` | $2 | $0.20 | $2.50 | $10 |
| MAX (high effort) | `claude-opus-5-5` | $4 | $0.20 | $5.00 | $20 |

| Explicit `@fable` (high effort) | `claude-fable-5-1` | $10 | $0.25 | $12.50 | $50 |

Automatic Claude routes with auth, security, database migration or production risk flags
use MAX/Opus 5.5. These backend-specific floors are under `policy.backend_risk_floors.claude`;
other backend policies keep their existing floors. An explicit tier tag overrides automatic floors.

New installs use these tiers. Existing custom mappings survive a plain reinstall. To adopt
these defaults explicitly and restart the bridge:

```sh
agentroute bridge install --update-models
```

Fable is registered as a separate backend and is never added to automatic tier mappings or
fallback chains. It uses the same bridge and subscription credential. Your plan must include
Fable access; its weekly scoped quota is distinct from the overall weekly limit. Use:

```text
@claude @normal continue               # Sonnet 5.5, medium effort
@claude @smart continue                # Sonnet 5.5, high effort
@claude @max continue                  # Opus 5.5, high effort
@fable @high solve this difficult task # Fable 5.1, explicit opt-in
@fable @ultra continue                 # Fable 5.1, Anthropic max effort
@claude continue                      # leave Fable
@auto continue                        # clear the manual backend preference
```

Like other provider tags, `@fable` persists until another backend tag or `@auto`. It is not a
one-request opt-in. Fable's automatic approval reviewer remains Haiku, so reviewing a command
never consumes Fable tokens just because the worker is Fable.

The bridge forwards Responses `reasoning.effort` as Anthropic `output_config.effort`:
`low`, `medium`, `high`, and `xhigh` pass through; `ultra` and `persistent` map to `max`;
`none` and `minimal` map to `low`. Opus 5.5 and Fable cannot disable thinking. Haiku 5.5 supports all effort levels and uses adaptive thinking. Legacy Haiku 4.5 still
advertises no effort levels.
Modern models use adaptive thinking and a default 128k output cap (thinking plus visible text);
an explicit caller cap is preserved. Top-level effort changes can invalidate Anthropic's prompt
cache; per-message cache-preserving effort is not implemented by this bridge.
See [Anthropic effort guidance](https://platform.claude.com/docs/en/build-with-claude/effort).

Haiku 5.5 accepts native forced tool choices; thinking is skipped for those calls.
Sonnet 5.5, Opus 5.5 and Fable 5.1 reject forced `tool_choice` modes. For required calls the
bridge uses `auto` plus a tool-call instruction; for a named call it exposes only that tool.
This is a prompting request, not a provider-enforced guarantee. Legacy models retain native
forced tool choice. Execution sandbox and approval checks remain enforced by Codex.

Haiku 5.5 has a 1M context window and 128k output cap. Its paired prices above apply
to prompts at or below / above 100,000 input tokens, including cache reads and writes.
AgentRoute applies the long-context multiplier to the whole request.

All rates are USD per million tokens, taken from Anthropic's published pricing page. They ship
with AgentRoute, so `agentroute usage` and `agentroute analytics` report Claude cost alongside
every other backend instead of treating those turns as free. The 1-hour cache TTL tier costs more
than the 5-minute rate shown above.

Long-context beta is negotiated per model, because Anthropic rejects it for Haiku. The bridge
keeps prompt images and images returned by tools such as `view_image`. Before sending a request,
it counts all images, including earlier turns and nested tool results. With more than 20 images,
it resizes any image larger than 2000 px on either axis to fit within 2000 × 2000 px, preserving
its aspect ratio. With 20 or fewer images, the maximum is 8000 px. These are
[Anthropic's image limits](https://platform.claude.com/docs/en/build-with-claude/vision).
Images within the limit are sent unchanged; local files and saved conversation history are
never modified. This lets a long screenshot-heavy session continue without manually clearing
its image history.

The Claude model descriptor has an empty instruction template. The routed Codex runtime removes
its `You are Codex` / `As Codex` identity claims from the outbound request copy sent to all
providers, and the bridge filters the same claims from forwarded Codex base instructions.
Other operating guidance and saved history are preserved. Subscription requests still include
the Claude Code identity block required by Anthropic for that credential; the API-key path has
no such block.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `agentroute doctor` fails `bridge` | run `agentroute bridge install`; read `~/.agentroute/bridge.log` |
| `bridge check` reports an auth error | run `claude` once, then `agentroute bridge refresh` |
| Codex says "Unknown model claude-..." | the backend is missing `model_catalog_url`; re-run `agentroute bridge install` |
| Turns fail with a connection error | the service is not running: `agentroute bridge status` |
| Port already in use | `agentroute bridge install --port <free port>` and re-run `backend-route` if needed |
| Image error mentions the 2000 px maximum for many-image requests | the bridge automatically resizes the outbound images; install the latest bridge version and restart it with `agentroute bridge install`, then retry the turn |
| Anthropic says a `tool_use` lacks a `tool_result` immediately after | upgrade AgentRoute to 0.5.59 or later and restart the bridge with `agentroute bridge install`; the bridge repairs interrupted calls and moves results before text in replayed history |
| Anthropic returns 429 | the bridge reports the Claude limit and reset when Anthropic supplies them; check `agentroute bridge usage` for the current window |
| Anthropic returns 403 | check the Claude Code login with `claude`; subscription credentials are outside Anthropic's terms for third-party clients |
