# Execution evidence and optional context experiments

AgentRoute remains a routing layer. Codex owns session history, compaction, tools,
and memory files; these features read existing evidence rather than implementing
a competing harness or silently importing other conversations.

## Execution analytics

`agentroute analytics` (or `--json`) includes execution receipts gathered by the
Stop hook: response count, cache fraction, observed tool completions/failures,
tool work, and observed compactions. Reads are bounded to an 8 MiB rollout tail
and matched to the exact turn. Old or missing receipts are unmeasured, not proof
of zero work. Tool durations may overlap; they are not subtracted from turn time
to invent a model-latency estimate. Tool payloads are not copied into the audit.

## Cache-aware routing (default: off)

```sh
agentroute context economics shadow
agentroute context economics off
```

Shadow mode records next-request estimates in the selection receipt without
changing the route. `retain` additionally permits retaining the active API model
instead of an automatic downgrade when its cached-input estimate is cheaper.
It requires a completed response less than five minutes old from the exact current
model/provider, at least 80% cached input and at least 10% estimated savings.
Thresholds are configurable under `routing.switching`.

The estimate assumes similar next-response sizes, cache reuse when staying, and
a cold cache after switching. These are assumptions, not measured future savings.
Subscription estimates are labeled API-equivalent and never trigger retention.
Explicit model/backend/reasoning choices, interrupted-turn affinity, and inherited
routes are not changed by this optimization. Capacity and policy limits still apply.

## Related-session context (default: off)

```sh
agentroute context search 'Azure compaction' --cwd /path/to/project
agentroute context mode shadow
agentroute context mode references
agentroute context mode off
```

Search previews existing `$CODEX_HOME/memories/rollout_summaries/*.md` artifacts.
If Codex has not produced those files, no references are available. This is bounded
keyword retrieval, not embeddings, a memory graph, or automatic session merging.

Only the same working directory is searched unless `context.related_cwds` explicitly
allows other projects. The current thread and artifacts older than 30 days are
excluded. Reads are limited by directory entries, file count, bytes, and a 150 ms
scan budget. At most three source references (2,400 characters by default) are
offered to the model; historical summary text is not injected. The references are
labeled untrusted historical evidence that requires current-state verification.

Shadow mode audits hashes/counts/timing only. Reference mode includes local source
paths and titles in model context, so enable it only for providers allowed to see
that project information. A credential-pattern check is a best-effort additional
filter, not a guarantee of secret detection. Disable immediately with `mode off`.
