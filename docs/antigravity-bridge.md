# Antigravity subscription backend

Runs Codex on your Antigravity subscription. Codex keeps the harness; `agy` only
supplies the model turn.

```sh
agentroute antigravity check
agentroute antigravity usage
agentroute antigravity install
```

The service listens on `127.0.0.1:8816`. Routes are unchanged until you opt in
with `agentroute backend-route normal antigravity` or a `@antigravity` prefix.

| Tier | Model | Effort |
| --- | --- | --- |
| FAST | `gemini-3.8-flash-low` | low |
| NORMAL | `gemini-3.8-flash-medium` | medium |
| SMART | `gemini-3.8-flash-high` | high |
| MAX | `gemini-4-hydrogen-high` | xhigh |

## Codex keeps the harness

Delegating a whole turn to `agy` would swap harnesses: your Codex instructions,
skills, tool definitions and approvals would be replaced by Antigravity's. Two
agents, two approval paths, and Gemini results that cannot be compared with any
other backend.

So `agy` runs in **plan mode**, where it executes nothing, and is asked one
question per turn: given this conversation and these tool definitions, reply
with prose or with exactly one tool call as JSON. Codex then executes that tool
and sends the result back, exactly as it does for the Gemini API and Claude
backends.

```text
{"tool":"shell","arguments":{"command":"ls -1"}}
```

What that means in practice:

- Codex's instructions, skills and instructions files apply unchanged.
- Codex runs every tool, under Codex's sandbox and approvals.
- Conversation state lives in the Codex session, not in agy.
- Token counts come from agy's own usage report, so this backend appears in
  `agentroute usage` and capacity reporting.

Verified: `codex exec -c model_provider=agentroute-antigravity` ran
`echo CODEX_TOOL_OK` through **Codex's** shell tool and reported its output.

## Why the turn is delegated at all

Google's Code Assist endpoints (`retrieveUserQuota`, `retrieveUserQuotaSummary`,
`generateContent`) answer `403 SUBSCRIPTION_REQUIRED (#3501)` to any client that
is not Google's own, and the subscription token carries `aicode` rather than the
Gemini API's `generative-language` scope. Driving `agy` keeps the request inside
Google's client: nothing impersonates it and no entitlement check is bypassed.

## Handover across providers: what Codex allows

Codex scopes a thread's history to its model provider. That has two
consequences, both verified rather than assumed:

- `codex exec resume <id>` with a different `model_provider` is refused outright:
  *"Cannot change this thread's model provider ... on resume."*
- Switching with an `@antigravity` prefix mid-thread reroutes the turn (the audit
  store shows `gemini-3.8-flash-low`), but the history does not travel with it, so
  the new provider answers without prior context.

That is why sessions that hop between providers hit "history contains an opaque
checkpoint from another or unknown provider". It is Codex's design, not this
bridge's.

Practical options:

- **Keep a thread on one provider.** Use `@antigravity` from the first turn, or
  never. History and tools work normally.
- **Compare models with the same harness** by giving each its own thread and the
  same opening prompt. Instructions, skills and tool loop are identical; only the
  model differs, which is what makes the comparison meaningful.
- **Hand the task over explicitly** with a written brief rather than relying on a
  silent history transfer.

## Tool execution and workspace

`agy` starts in `AGENTROUTE_ANTIGRAVITY_CWD` (default: your home directory). Plan
mode means nothing it could reach actually runs; Codex executes the returned
call in the Codex workspace. Set the variable anyway if you want any agy-side
reads to resolve relative to a repository.

One tool call per turn is requested, so `parallel_tool_calls` is declared `false`
for these models. Codex then sends sequential tool turns, which the protocol can
represent.

## Usage and limits

`agentroute antigravity usage` prints what `agy -p "/usage"` reports, cached for
ten minutes:

```text
Antigravity subscription (2026-11-26T12:16:06Z):
  Gemini Models              88.0% remaining · resets 2026-12-03T07:15:35Z
  Claude and GPT models     100.0% remaining · resets 2026-12-03T06:55:01Z
```

`agentroute capacity status` reports the Gemini group as subscription state with
its reset time, so an exhausted allowance no longer looks healthy.

Token volume is priced with API-equivalent Gemini rates. The subscription is
flat-fee, so those numbers measure work done, not what you owe.

## Relations to the Gemini API backend

`agentroute-gemini` and this backend are different products with separate
quotas: the Antigravity allowance is a weekly percentage, while the Gemini API is
per-project RPM/TPM/RPD plus your own dollar budget. Spending one does not touch
the other.
