# Antigravity subscription backend

Runs Codex on your Antigravity subscription. The bridge drives the `agy` CLI
through its documented structured interface (`--output-format stream-json`) and
translates the result into Responses events.

```sh
agentroute antigravity check     # CLI, sign-in and weekly allowance
agentroute antigravity usage     # the subscription allowance on its own
agentroute antigravity install   # register the backend and start the service
```

The service listens on `127.0.0.1:8816`. Routes are unchanged until you opt in
with `agentroute backend-route normal antigravity`.

| Tier | Model | Effort |
| --- | --- | --- |
| FAST | `gemini-3.8-flash-low` | low |
| NORMAL | `gemini-3.8-flash-medium` | medium |
| SMART | `gemini-3.8-flash-high` | high |
| MAX | `gemini-4-hydrogen-high` | xhigh |

## Why this delegates instead of calling Google

Two things block a direct call, both verified rather than assumed:

- Google's Code Assist endpoints (`retrieveUserQuota`, `retrieveUserQuotaSummary`,
  `generateContent`) answer `403 SUBSCRIPTION_REQUIRED (#3501)` to an ordinary
  token. Only Google's own clients are licensed.
- The subscription's OAuth token carries `aicode`, `cclog`, `cloud-platform` and
  `experimentsandconfigs` — not the Gemini API's `generative-language` scope, and
  a refresh token never gains scopes after consent.

Delegating to `agy` keeps the request inside Google's client, so no identity is
impersonated and nothing about the entitlement is bypassed. The bridge adds what
the CLI does not give Codex by itself:

- **Streaming.** `text_delta` events become `response.output_text.delta`, so
  output appears as it is produced instead of after the whole turn.
- **Accounting.** agy reports `input_tokens`, `output_tokens`,
  `thinking_tokens` and `cache_read_tokens` per turn; these become Responses
  usage, which Codex records in the audit store like any other backend. Thinking
  tokens are billed as output, and cache reads are reported as cached input.
- **Continuity.** agy conversation IDs are mapped to the Codex thread, so a
  follow-up turn resumes the same conversation instead of re-explaining context.

## Tool execution

Antigravity runs its own tools inside a turn. Codex therefore sees a **text-only**
model on this backend (`tool_calling: none`): handing Codex tools as well would
let two agents act on one workspace with no single approval path. The tools agy
uses, and its permission mode, are configured on the agy side.

This is the tradeoff for using the subscription: agy's tool loop governs, not
Codex's sandbox. If you need Codex's approval path to gate every action, use a
backend that Codex drives directly.

Because Codex does not send its working directory in the request, the bridge runs
agy in a configured workspace:

```sh
export AGENTROUTE_ANTIGRAVITY_CWD=/path/to/repo   # default: ~/workspace
export AGENTROUTE_ANTIGRAVITY_MODE=plan           # optional agy --mode
```

These are copied into the launchd/systemd definition at install time, because
services do not inherit your shell environment.

## Usage and limits

`agentroute antigravity usage` prints what `agy -p "/usage"` reports, cached for
ten minutes:

```text
Antigravity subscription (2026-11-26T12:16:06Z):
  Gemini Models              88.0% remaining · resets 2026-12-03T07:15:35Z
  Claude and GPT models     100.0% remaining · resets 2026-12-03T06:55:01Z
```

`agentroute capacity status` shows the same Gemini group as a subscription
state, including the reset time, so an exhausted allowance reports as such
instead of looking healthy.

Token volume is priced with API-equivalent Gemini rates. The subscription itself
is flat-fee, so those numbers are informational: they measure how much work ran,
not what you owe.

## What is separate from the Gemini API backend

`agentroute-gemini` and this backend are different products with different
quotas. The Antigravity allowance is a weekly percentage; the Gemini API is
per-project RPM/TPM/RPD plus your own dollar budget. Spending one does not touch
the other. If you want Gemini with Codex-driven tools, use the Gemini API
backend; if you want the subscription, use this one.
