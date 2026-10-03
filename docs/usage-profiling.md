# Usage and context profiling

The profiler answers two separate questions:

- **What was consumed across requests?** Provider input, cache reads, cache writes, output,
  configured API-equivalent cost estimates, model and thread attribution.
- **What was present in each request?** Instructions, AGENTS.md, memory, skill catalog,
  loaded skill blocks, conversation history, tools, tool calls/results, and media.

It also sums each section's size across requests to reveal repeated input during tool loops.
A section can occupy 20 KB in the current context and contribute 2 MB across 100 requests.
Cache reads reduce the price of repeated input; they still occupy context.

## Enable and use

```sh
agentroute context profile on
```

Start a **new** Codex session through the AgentRoute terminal launcher or routed Desktop app.
For Desktop, fully quit and reopen the app so its app-server inherits the capture setting.
The launcher enables Codex's existing `CODEX_ROLLOUT_TRACE_ROOT` recorder. No Codex rebuild
is required on the current pinned runtime. Older runtimes without that recorder can still
produce Claude bridge receipts, but other providers will have no request captures.

The installed Claude bridge must run the profiler version of AgentRoute; restart its service
after upgrading. It checks the capture setting for every request and records the actual
Anthropic payload **after** translation from Codex, retaining counts instead of prompt text.
It also measures the Codex payload before translation for comparison in JSON reports.

```sh
agentroute context profile status
agentroute context profile report
agentroute context profile report --days 0.04
agentroute context profile report --session <session-or-child-thread-id>
agentroute context profile report --turn <turn-id> --json
agentroute context profile report --output /tmp/agentroute-profile.json
agentroute context profile off
```

`off` stops capture in future launched sessions and bridge requests. A running Codex process
already holding `CODEX_ROLLOUT_TRACE_ROOT` continues tracing until restarted. Explicit
`AGENTROUTE_PROFILE_CAPTURE=1` or `CODEX_ROLLOUT_TRACE_ROOT` environment settings remain active.

`--root` reads another profiling directory containing `traces/` and `bridge/`. An explicitly
configured Codex trace root is preserved by the launcher and is not automatically searched
by the report: place its bundles under the selected profiling directory's `traces/`.

For a session recorded before capture was enabled:

```sh
agentroute context profile report --rollout ~/.codex/sessions/.../rollout-....jsonl --days 7 --json
```

This recovers per-response token receipts when available. Those receipts do not prove the
actual model or assembled context, so unmatched responses are explicitly unpriced and have
no component breakdown. Lost historical cache counters cannot be reconstructed.

## Interpreting the report

**Measured usage.** Counts come from each provider completion, not cumulative turn/session
counters. The reader joins Claude sidecars to Codex traces by inference ID or response ID,
so the same call appears once. WebSocket context is reconstructed from the previous response
chain; absent prefixes are flagged as partial. Parent and child threads are listed separately.

Anthropic `input_tokens` excludes cache reads and writes. The bridge exposes Responses API
input as `input + cache_read_input + cache_creation_input`, with the latter two counters as
subsets. This also fixes subsequent Codex/AgentRoute usage receipts outside profiling mode.
Cache TTL details are preserved in bridge sidecars. Output includes thinking when the provider
counts it there; no separate exact Anthropic thinking-token count is invented.

**Cost estimates.** USD uses your configured `pricing.models` rates. Input is split into fresh
input, cache reads and writes, and output; reasoning is not charged a second time. Known
5-minute and 1-hour cache writes use their corresponding rates. If write TTL is missing,
the configured default write price is used and the report records the assumption. Missing
model prices, missing 1-hour write prices, and missing usage are unpriced. Totals include only
priced components and may be incomplete. JSON includes rates and assumptions for every call.
`cache_write_1h_per_million` is optional in model pricing; existing saved model entries need
this field to price 1-hour writes. The bundled Claude defaults supply it.

These are **API-equivalent estimates**, not subscription charges or quota weights. They also
require correct rates for the actual service tier and any long-context pricing. See
[Anthropic's caching accounting](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).

**Context.** Text section sizes are measured UTF-8 bytes; structured tool definitions and
arguments are measured from JSON serialization. Section token counts use bytes / 4 and are
estimates. They are not a provider tokenizer or an exact allocation of billed input tokens.
Provider-reported total input is the measured token load of that request. Marker-based
AGENTS/skill/memory attribution is a structural hint; quoted markers can affect classification.
Skills read through ordinary tools may appear under tool results rather than loaded skill blocks.

Images/audio are counted separately, including encoded payload size. They are not treated as
base64 text tokens. Opaque provider state is counted separately too. Provider input remains
necessary to see their real context impact. A bridge window comparison uses its advertised
model window; other records may not report one. JSON retains both largest and latest captured
context, while the terminal shows the largest captured context and input growth per thread.

**Coverage.** Recorded classifier usage is read from the existing audit database without
initializing or migrating it. These calls are included in cost totals, with unavailable context.
Some Codex compaction paths disable inference tracing; the profiler recovers their usage from
associated rollout receipts, while leaving unknown models unpriced. Non-streaming compaction
captures with no usage receipt are counted as unmeasured. Cancelled/failed streams can have
partial counters or none; missing receipts are not evidence of zero consumption. Concurrent
traffic, unrecorded classifier fallback attempts, requests outside AgentRoute, and provider-side
work with no returned receipt are not fully attributable by this tool.

## Investigating a quickly exhausted Claude window

Inspect request count, fresh-input share, cache-hit share, output, the biggest repeated sections,
and parent/child thread activity. Compare the captured account quota samples and their reset
times. Samples are observations of a **shared account**, not exact per-request quota debits;
other sessions and sampling delay can affect them. Anthropic's private subscription formula
cannot be derived from published API prices. The report can show which work coincided with
consumption and how much input/output it generated; it cannot convert tokens into exact
remaining subscription minutes.

## Local data and retention

Capture is opt-in. The private `~/.agentroute/profiling/` directory contains:

- `traces/`: native Codex bundles, including raw request/response text and tool payloads.
- `bridge/`: counts-only provider receipts with model, IDs, sizes, token counts, and quota samples.
- `enabled`: capture marker.

No profiling data is uploaded. Generated reports omit request text, retaining tool/skill names
and session correlation IDs. JSON exports are private files. Turning capture off retains data;
remove only the desired local trace/receipt folders when finished. Do not share raw trace bundles
without reviewing their contents. The profiler does not change prompt caching or skill selection.


## Storage limits (0.5.63 / runtime v50)

Profiling remains opt-in. Raw captures can contain full prompts, tool output and
model responses. The native writer shares a 512 MiB budget across processes and
stops capture when it is exhausted; model execution continues. Removing the
managed `profiling/enabled` marker through `agentroute context profile off` also
stops writes during a running v50 session. Older runtimes need a restart.

Old traces are pruned to a 512 MiB target and seven-day retention. Counts-only
bridge receipts use a 64 MiB target and 30-day retention. A bundle modified within
the last hour is protected from pruning, so legacy unbounded captures may exceed
the target until their processes restart. Reports cover retained captures only;
a missing request is not proof of zero usage or savings.

```sh
agentroute storage status
agentroute storage prune --build-cache --desktop-backups
```

This removes known inactive Rust build output and retains two Desktop rollback
copies. It preserves Codex chats, configuration, credentials and source edits.
Cleanup refuses build paths while Cargo/rustc are running. Source installs now
use temporary build directories and clean them on failure as well as success.
`AGENTROUTE_KEEP_BUILD=1` explicitly retains output for development.
