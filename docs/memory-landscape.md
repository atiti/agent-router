# Agent memory: landscape and an AgentRoute direction

Research date: 2026-09-25. This is a proposal, not a claim that the proposed
system ships in AgentRoute. Codex observations are pinned to 0.157.0; external
product observations are from their current documentation. No comparative
benchmark was executed as part of this review.

## Recommendation

Keep Codex responsible for its conversation, compaction, memory production and
tool loop. Extend AgentRoute's small context adapter into a **context selector**:
find the smallest useful set of evidence from related work, explain why it was
selected, and respect project/provider boundaries before sending anything.

Do not start by copying every conversation into another vector database or
building another summarizing agent. First measure what Codex's native memory
already supplies and what our current reference-only adapter adds.

## What Codex already does

The inspected 0.157 source has substantially more than context compaction:

- A background long-term-memory pipeline, gated by the `memories` feature
  (stable, but false in the source defaults). Eligible persistent root sessions
  start it; ephemeral and subagent sessions do not. A state store is required.
- Phase 1 extracts raw memory and a summary from eligible idle rollouts, with
  bounded concurrency, job leases, retry backoff and generated-secret redaction.
- Phase 2 consolidates selected outputs into a filesystem memory workspace.
  Selection considers usage and retention; the process uses a global lease and
  a Git baseline/diff. A restricted consolidation agent updates higher-level
  knowledge rather than simply concatenating every transcript.
- A read path supplies a small summary and directs the agent to search a registry,
  then open relevant summaries, skills or original evidence. It explicitly asks
  for stale-state verification and memory citations. Local memory search is
  text matching, not an entity graph or a semantic-vector retrieval service.

Sources: [startup implementation](https://github.com/openai/codex/blob/rust-v0.157.0/codex-rs/memories/write/src/start.rs),
[pipeline overview](https://github.com/openai/codex/blob/rust-v0.157.0/codex-rs/memories/README.md),
[feature defaults](https://github.com/openai/codex/blob/rust-v0.157.0/codex-rs/features/src/lib.rs),
[read instructions](https://github.com/openai/codex/blob/rust-v0.157.0/codex-rs/ext/memories/templates/memories/read_path.md),
[local search](https://github.com/openai/codex/blob/rust-v0.157.0/codex-rs/ext/memories/src/local/search.rs).
Some paths in the upstream overview predate the crate split; the actual source
locations above take precedence.

Extraction and consolidation have their own model settings:
`memories.extract_model` and `memories.consolidation_model`. The provider defaults
in this release are `gpt-5.6-luna` and `gpt-5.6-terra`. Successful foreground
routing does not establish that these background calls work with a particular
Azure deployment or account. Verify their configuration before enabling memory.
Sources: [settings](https://github.com/openai/codex/blob/rust-v0.157.0/codex-rs/config/src/types.rs),
[provider defaults](https://github.com/openai/codex/blob/rust-v0.157.0/codex-rs/model-provider/src/provider.rs).

Compaction and memory solve different problems: compaction bounds the active
conversation; long-term memory makes selected knowledge from prior sessions
available to later work. Neither automatically guarantees the right evidence
is retrieved or that old facts remain true.

AgentRoute currently only provides bounded keyword matching over existing
rollout summaries, exact-working-directory/explicit-allowlist scoping, and
optional source references. It does not yet provide semantic retrieval, a
memory graph, conflict resolution or automatic cross-session merging. It is
off by default. See [current feature boundaries](efficiency-and-context.md).

## What others are doing

| System | Approach | Useful lesson |
| --- | --- | --- |
| Claude Code | Human-authored instructions and repo-scoped automatic notes; a small memory prefix loads at startup, with more files available on demand. Path-scoped rules and skills reduce unnecessary context. | Separate durable instructions from learned observations. [Docs](https://code.claude.com/docs/en/memory) |
| Letta Code | Git-backed context repositories, progressive disclosure, background reflection and isolated worktrees for concurrent memory work. | Inspectability, versioning and concurrency matter as much as retrieval. [Context repositories](https://www.letta.com/blog/context-repositories/) |
| LangChain Deep Agents | Filesystem-backed persistent memory, scoped backend namespaces, eager or on-demand reads, optional background consolidation. | Storage/access policy and context assembly can be separate layers. [Docs](https://docs.langchain.com/oss/python/deepagents/memory) |
| Zep / Graphiti | Temporal knowledge graph built from episodes, entities and relationships. | Track when a fact was true, not just when its text was embedded. [Paper](https://arxiv.org/abs/2501.13956) |
| Mem0 | Extraction, consolidation and retrieval, with an additional graph-based variant. | A compact memory service is an alternative to replaying long histories. [Paper](https://arxiv.org/abs/2504.19413) |
| A-MEM | Zettelkasten-inspired linked notes whose attributes and connections evolve as evidence arrives. | Multiple navigable relationships can outperform a single chronological list. [NeurIPS 2025 paper](https://arxiv.org/abs/2502.12110) |
| Hindsight | Distinguishes world facts, experiences, entity summaries and beliefs; retain/recall/reflect operations. | Do not silently promote inference into verified fact. [Paper](https://arxiv.org/abs/2512.12818) |

These are documented designs, not proof that any one works best for our tasks.
Published memory benchmark gains are not directly comparable across different
models, ingestion costs, tasks, context budgets and latency budgets.

## Direction of travel

My synthesis of those sources: the field is moving from indiscriminate transcript
recall toward selective disclosure, background consolidation, typed evidence,
temporal updates and auditable memory. Graphs are one implementation, not the
goal. Plain files remain important because agents and humans can inspect them.

Evaluation is also moving beyond remembering user preferences.
[LongMemEval](https://arxiv.org/abs/2410.10813) includes temporal reasoning,
knowledge updates and abstention. The newer, work-in-progress
[LongMemEval-V2](https://arxiv.org/abs/2605.12493) tests environment experience:
state changes, workflows, recurring gotchas and incorrect premises. Its reported
results also expose the latency cost of agent-driven evidence gathering.

## What multidimensional should mean here

One evidence item should be accessible through several independent dimensions:

| Dimension | Example question it answers |
| --- | --- |
| Ownership and scope | Which person, client, project and approved provider may use it? |
| Entity and dependency | Which service, repository, file, deployment or issue does it affect? |
| Time and version | Is it true now, superseded, or only valid before a release? |
| Knowledge type | Is this a user decision, verified observation, procedure, failure or hypothesis? |
| Task and outcome | Which goal did this advance; did the attempted fix actually work? |
| Provenance | Which session, commit, test or external observation supports it? |

For example, the Azure compaction investigation should be retrievable by
provider, compaction symptoms, affected release, code path, related ticket and
successful regression test. A later task must not receive an old broken-state
description as if the fix had never shipped.

## Where I see room to innovate

These are proposed differentiators, not claims that nobody else offers them:

1. **Version-aware engineering evidence.** Distinguish a completed fix on branch A
   from a deployed fix on main. Track supersession without erasing the evidence.
2. **Measured context selection.** Optimize task success and time-to-first-useful
   action per token and millisecond, including ingestion and maintenance costs.
3. **Provider-aware disclosure.** The same project can route to a subscription,
   Azure or a local endpoint; allowed context should be checked against the
   destination before retrieval results leave the machine. This must also cover
   later provider switches: evidence or derived answers can persist in the thread.
   A simple initial design is to constrain eligible providers for the whole thread
   once restricted evidence enters it. Removing only the original retrieved
   snippet is not proof that derived private information has been removed.
4. **Visible memory decisions.** Show what was loaded, why, source age, evidence
   type and how to reject or correct it. Never turn remembered text into new
   approval authority or higher-priority instructions.
5. **Related-session/subagent handoffs.** Share narrow verified findings and open
   dependencies, without importing the parent conversation into every worker.
6. **Joint model/context budgeting.** Test whether better evidence lets a cheaper
   model solve the task reliably. Never assume retrieval justifies a downgrade;
   compare end-to-end correctness and total cost, including memory maintenance.

## Smallest credible experiment

1. Establish a native-Codex baseline. Verify generation and retrieval actually run
   under our configured providers; do not infer this from files already on disk.
2. Build an offline test set from approved historical work: prior decisions,
   repeated failures, superseded fixes, related repos and questions with no answer.
   Exclude the target session and all future evidence to avoid evaluation leakage.
3. Add a rebuildable local index over approved native memory artifacts and curated
   workspace knowledge. Start with SQLite/FTS plus explicit metadata and links.
   Add embeddings or graph traversal only if they improve held-out results.
4. Retrieve in shadow mode. Compare native-only, current keyword references,
   metadata-aware retrieval and one external candidate under equal budgets.
5. Offer a few source references first; fetch bounded evidence on demand. Integrate
   through hooks/tools, keeping upstream Rust changes minimal. Native memory and
   the adapter need a combined budget and deduplication, not two competing prompts.

Suggested acceptance criteria: zero cross-scope disclosure in adversarial tests;
fewer repeated investigations without worse task correctness; explicit abstention
for missing evidence; improved stale-fact rejection; measured p50/p95 retrieval
latency and total tokens including background processing. Record these as a
predeclared experiment, not savings claims before measurement.

No new memory feature has been enabled or third-party memory service connected
as part of this research.
