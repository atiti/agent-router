# Grok backend

## Subscription login

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

## Public xAI API key

An xAI API key can use the same loopback bridge and Codex tool translation:

```sh
# Supply the key through a private environment variable, then import it:
agentroute grok credential-import XAI_API_KEY
agentroute grok install --credential api-key
agentroute grok status
```

The explicit import reuses AgentRoute's secure credential writer in a separate owner-only file, `~/.agentroute/grok-api-credentials.env`. The background service reads it for each request, so importing a rotated key requires no reinstall. Neither the installer nor foreground `grok serve` implicitly uses an inherited `XAI_API_KEY`; import the selected environment variable first, then unset that import variable before launching Codex. This prevents a different inherited key from overriding the intended account and keeps the upstream key outside the ordinary launch credential file. Secrets never enter service definitions, command arguments, provider catalogs or diagnostics. Installation checks a fresh challenge bound to both intended credentials, the new service instance and its listening port before publishing the provider configuration.

Installation also creates a separate random local-client credential named `AGENTROUTE_GROK_BRIDGE_KEY` in the ordinary owner-only `backend-credentials.env` file. The generated Codex provider references its name, and the usual AgentRoute launch wrapper loads it. API-mode inference rejects requests without this local credential. The bridge never forwards it to xAI, and an xAI API key cannot authenticate the local endpoint. A subscription service rejects API-client bearer requests so an already-loaded API session cannot silently spend subscription allowance after a mode switch. Restart the session after changing credential modes.

API-key mode sends credentials only to `https://api.x.ai/v1`; it does not use
Grok CLI authentication or its subscription proxy. The CLI is not required.
The existing patch, function namespace and encrypted-reasoning translations
apply to both modes. This avoids the public API's rejection of Codex's `custom`
patch-tool schema. Requests remain streaming with `store=false` and redirects
are refused. Provider retention terms still apply to API traffic.

A new API backend uses `grok-4.7` at every tier, with low, medium, high and xhigh
reasoning respectively and a 500,000-token catalog context. The unchanged shipped subscription tiers migrate to these API defaults. Custom public-model tiers and context overrides are preserved; an existing subscription-only FAST or review model is changed to `grok-4.7`. Public `grok-4.6` and `grok-4.5` are also
supported; subscription-only `grok-4.7-build-fast` is rejected in API mode.
Confirm actual model access and limits in the account's `/v1/models` catalog.

The API key's funding and limits belong to the account that issued it. Installing
the bridge does not establish that an X/Grok subscription or startup admission
funds API requests. `status` identifies the active credential mode; `/usage`
reports API billing as unavailable and never sends the key to subscription
billing. Responses token usage still passes through to Codex. Standard local
token-cost estimates and configured budget ceilings remain estimates; they are
not an API credit balance. HTTP 429 records an account-bound retry cooldown
without replaying the active request.

API installation refuses to replace an existing Grok service implicitly. Use `status` to inspect it and the normal user-service restart to restart unchanged credentials. To change credential modes, stop and disable the existing backend with `agentroute grok uninstall`, then install the selected mode and start a fresh Codex session. API key rotation needs only `credential-import` and does not reinstall the service. Automatic tier routes are preserved.

The installer renders and validates configuration in temporary files before starting the service. Only after health and the credential proof succeed does it publish the prepared files atomically, checking their original contents to preserve concurrent changes. A failed first installation leaves no active API route. Credentials and saved sessions remain outside that transaction.

The function-compatible provider exposes tools directly. A large installed connector catalog can therefore occupy most of the model context and cause repeated compaction. For local file work, use supported per-session controls such as `codex --disable apps -c 'mcp_servers.NAME.enabled=false'` for unnecessary configured MCP servers, then select `@grok`. These controls do not change other sessions. Enabling Codex-only tool-search protocols is not inferred from ordinary API function support.

## Subscription capacity monitoring

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

The subscription mode appears in the standard backend capacity table and JSON with used/remaining
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
