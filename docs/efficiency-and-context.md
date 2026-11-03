# Execution evidence and optional context experiments

AgentRoute remains a routing layer. Codex owns session history, compaction, tools,
and memory files; these features read existing evidence rather than implementing
a competing harness or silently importing other conversations.

## Context diagnostics

`agentroute doctor` checks tier, direct-model and reviewer limits, including the
active ChatGPT account's target overrides. It compares those targets with the
written provider configuration and effective launch catalog. Conflicting explicit
limits for the same model and missing or mismatched applied windows remain failures.
Review-only models receive the same generated provider limits as worker models.

The shared ChatGPT catalog maximum is not an independent limit for an Azure or
other routed API deployment. Those maxima remain unknown until established by
their own source; an explicit custom catalog retains its maximum checks. A warning
that the provider limit is unverified is not a successful maximum-capacity test.
Ordinary inference, native context selection and compaction behavior are separate
checks. Capacity management and model pricing warnings also remain separate from
authentication and context correctness.

## Execution analytics

`agentroute analytics` (or `--json`) includes execution receipts gathered by the
Stop hook: response count, cache fraction, observed tool completions/failures,
tool work, and observed compactions. Reads are bounded to an 8 MiB rollout tail
and matched to the exact turn. Old or missing receipts are unmeasured, not proof
of zero work. Tool durations may overlap; they are not subtracted from turn time
to invent a model-latency estimate. Tool payloads are not copied into the audit.

Version-2 receipts also read current typed command, MCP, dynamic-tool, patch,
collaboration and compaction completion items. Duplicate legacy/current records
for one call count once. Parser version alone does not prove that tool events
were recorded: absent tool events and timings remain unobserved, and historical
version-1 receipts retain their coverage warning.

Unpriced model rows display `unpriced`, not a zero cost. Cost comparisons include
the same priced answer turns and their classifier overhead; total classifier
overhead is also shown separately. With no comparable priced turns, savings is
unavailable. Classifier errors without latency are counted without assuming that
they happened before an HTTP request. All costs remain token-price estimates,
not invoices or measured savings against an actual alternative workload.

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

Retrieval uses a small built-in conversational stop-word list and weighted lexical
evidence: title matches count three points, section-heading matches count two, and
incidental body matches count one. Candidates need at least two distinct overlapping
terms and a weighted score of four (a single term repeated in both the title and
body also qualifies). This precision guard is not semantic search; inspect
`agentroute context search` previews and treat misses as possible. The preview reports
matched terms and fields to make ranking explainable. The routing audit stores only
counts, timings, and reference hashes; AgentRoute does not currently observe whether
Codex opened a referenced file or whether it helped, so usefulness still needs a
small human-judged evaluation set.
