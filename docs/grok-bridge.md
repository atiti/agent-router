# Grok subscription backend

Install the [official Grok CLI](https://github.com/xai-org/grok-build), run
`grok login`, and confirm `grok models` works. Then:

```sh
agentroute grok install
agentroute grok status
```

Start a new Codex session and select `@grok`. Use `@grok @fast`, `@grok @normal`,
`@grok @smart`, or `@grok @max` to select a tier; combine with `@high` or `@xhigh`
to override reasoning. Installation preserves automatic tier routes. To opt in:

```sh
agentroute backend-route normal grok
# Or route every tier through Grok:
agentroute backend-default grok
# Change a tier's model using the standard backend command:
agentroute backend-enable grok --fast-model grok-4.7
```

| Tier | Model | Reasoning |
| --- | --- | --- |
| FAST | `grok-4.7-build-fast` | low |
| NORMAL | `grok-4.6` | medium |
| SMART | `grok-4.7` | high |
| MAX | `grok-4.7` | xhigh |

`grok-4.5` is also supported. Fast requires a paid account with access to that
model. Check `grok models` before changing mappings. Reinstall preserves existing
tier choices and re-enables a previously uninstalled backend.

The catalog declares a conservative **256,000-token context window**, matching
the CLI's fallback window. The public API advertises larger windows for some
models; this bridge does not infer a larger subscription entitlement from those
limits. Operator overrides under `capabilities.models.<id>.context_window` or a
backend model target's `context_window` use the standard generated Codex catalog.
Codex uses local summary compaction for this provider.

## Capacity monitoring

```sh
agentroute capacity enable
agentroute capacity status
agentroute capacity status --json
agentroute capacity status --no-probe

# Permit automatic routes to leave Grok at the configured capacity threshold:
agentroute capacity fallback grok gpt
# Optional local cost-estimate ceiling, independent of the subscription allowance:
agentroute capacity budget grok --daily 10
```

Grok appears in the standard backend capacity table and JSON with used/remaining
percentages and a reset timestamp in machine-local time. The bridge reads the
same `billing?format=credits` endpoint as Grok CLI. New percentage/period fields
are preferred; legacy monthly limit/used fields are supported. Malformed measurements
and reset timestamps are rejected. Billing reads
consume no inference tokens. Samples are bound to the signed-in account, saved
with owner-only permissions, and cached for 60 seconds. Stale or failed reads
show `unknown`, rather than a healthy zero. `--no-probe` makes no live requests.

Routing uses the standard warning, switching and recovery thresholds. Automatic
routes use the configured fallback chain when Grok is exhausted; explicit
`@grok` and sticky Grok routes block until capacity recovers or you choose `@auto`
or another backend. An upstream HTTP 429 records a bounded retry cooldown for
subsequent turns. An active request is never replayed by the bridge. Unknown
telemetry permits routing, consistent with the standard subscription behavior.
The shared pool can also be consumed by other Grok or Cursor clients; local token
receipts cannot predict its remaining balance.

## Usage and cost estimates

Responses usage counters pass through to Codex and its standard per-turn audit.
`agentroute stats`, analytics, and daily/monthly ceilings use the existing token
cost calculation. Reasoning output is included in output tokens and is counted
once. Published rates below were checked on **2026-11-01** against
[xAI's pricing](https://docs.x.ai/developers/pricing), in USD per million tokens:

| Model | Input | Cached input | Output | Long-context multiplier |
| --- | ---: | ---: | ---: | ---: |
| `grok-4.7`, `grok-4.6` | $2 | $0.50 | $6 | 2× |
| `grok-4.5` | $2 | $0.30 | $6 | 2× |
| `grok-4.7-build-fast` | unpublished | unpublished | unpublished | unpublished |

The public price list does not currently list `grok-4.7-build-fast`. Its tokens
are audited, but standard cost reports flag it as unpriced and exclude it from
cost totals until you configure verified rates under `pricing.models`.
Map FAST to a model with a published price when relying on local cost ceilings:
`agentroute backend-enable grok --fast-model grok-4.7`.
The priced models use an inclusive long-context threshold of 200,000 prompt
tokens (`long_context_threshold_inclusive: true`). Prices are configurable under
`pricing.models`. These are token-rate equivalents; subscription charges and
remaining allowance come from your account. Standard reports aggregate a turn's
observed tokens, so estimates can differ from request-level charges when several
requests cross a context threshold in aggregate. Server-side tool fees are not
included. Ephemeral Codex runs do not persist the transcript needed for standard
per-turn auditing.

The bridge defaults `max_output_tokens` to 16,384 when the caller does not specify a limit. An explicit caller limit takes precedence. This bounds runaway generations; an incomplete response is still reported as an error by Codex and may require retrying in a fresh or shorter context.

## Service, authentication and troubleshooting

The proxy listens only on `127.0.0.1:8092` (`--port` overrides it). It runs as a
separate LaunchAgent on macOS or `systemd --user` service on Linux.
`agentroute grok uninstall` stops the service and disables the backend. Existing
automatic routes pointing at Grok should be reassigned before uninstalling.
For foreground operation:

```sh
agentroute grok serve --grok-binary /absolute/path/to/grok
```

The installed service uses the absolute CLI path discovered during installation.
It carries explicit `GROK_HOME`, `GROK_AUTH_PATH`, and `AGENTROUTE_HOME` overrides
from installation. Reinstall after moving the CLI or changing these paths.

The bridge reads the single first-party OAuth login from `~/.grok/auth.json`.
Credentials never enter AgentRoute config, command arguments, catalogs, snapshots,
or logs. Near expiry it invokes `grok models`, letting Grok own refresh, locking,
and rotation, then rereads the file. Each upstream request uses one credential snapshot
for authorization and account attribution, including its rate-limit cooldown.
Custom enterprise issuers and ambiguous
first-party logins are rejected. If authentication fails, run `grok login` and
`grok models`, then retry. No API key is required.

Requests use Grok's CLI proxy Responses endpoint and its required token-auth,
model override, and installed CLI version headers. Namespace tools retain their
identities across the wire; the patch tool becomes an ordinary upstream function
and is restored to Codex's custom tool downstream. Encrypted reasoning is retained;
the incompatible Codex `content: null` field is removed on replay. Codex retains
tool execution, sandbox, and approval authority.

Grok can overlap a message with search and function-call events. The bridge keeps
each output item's events contiguous for Codex while streaming the first item
immediately. Overlapping items are delivered when the preceding item finishes;
this prevents tool completion events from clearing an answer that is still arriving.

Browser-origin requests and non-loopback host headers are rejected. Authenticated
upstream requests do not follow redirects. Upstream HTTP codes are preserved
without exposing upstream diagnostics or prompt content. `agentroute doctor`
checks bridge health. `grok status` checks the local service without inference;
it does not prove upstream authentication. A bounded `@grok @fast` file-read
prompt checks authentication, streaming and tool continuation together.

Validated with Grok CLI 1.0.70 and AgentRoute's Codex 0.165 runtime. This integration
depends on the CLI proxy contract and applicable xAI account terms; availability,
models, and quota behavior can change.
