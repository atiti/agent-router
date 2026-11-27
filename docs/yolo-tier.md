# Proposal: an explicit YOLO tier above MAX

Status: proposed; no runtime behavior changes in this PR. `@yolo` is not yet a
supported command. This document defines the implementation and acceptance scope.

## Outcome

Add an opt-in fifth tier for work where the user deliberately chooses the strongest
available model and its highest supported reasoning effort. Keep ordinary routing
within FAST, NORMAL, SMART and MAX. YOLO selects a model and effort; it does not
change tool permissions, approvals, sandboxing, budgets or account access.

The requested distinction is MAX at GPT-6 Astra/high versus YOLO at Astra's maximum
supported effort. Preserve existing users' customized MAX mappings; do not impose
this particular MAX setting on every installation.

## Proposed model and effort choices

| Backend | YOLO model | Effort | Evidence and qualification |
| --- | --- | --- | --- |
| ChatGPT (`gpt`) | GPT-6 Astra, using the exact model available to the selected account | `ultra`, or `max` when that is the provider's supported spelling | Current source defaults name `gpt-6.2-astra`; an existing account may instead expose `gpt-6-astra`. Verify the selected account's catalog and an actual request before enabling the mapping. |
| Claude subscription (`claude`, explicit `fable`) | `claude-fable-5-2` | `max` at the Claude boundary | Current bridge registration already maps its MAX model to Fable 5.2. The router represents Claude `max` as `ultra`, and the bridge normalizes it back to `max`. Preserve and test that translation. |
| Grok subscription (`grok`) | `grok-4.7` | `xhigh` | Reuse the highest bundled Grok setting. There is no evidence here for a stronger Grok effort; do not invent `ultra` or `max` support. |
| Azure (`azure`) | The configured GPT-6 Astra deployment | Highest explicitly verified supported effort (`ultra` or `max`) | Deployment availability and accepted effort are independent of ChatGPT account access. Azure is the last automatic fallback. |

Grok YOLO can intentionally equal Grok MAX. A tier expresses the requested level
across providers; it does not establish that every provider has another stronger
model. These settings are recommendations, not comparative quality benchmarks.
GPT model generation and provider wire effort must be explicit in the final
configuration and execution receipt; a generic family label is insufficient.

Do not silently reduce an unavailable GPT maximum to `high` or `xhigh`. Exclude that
YOLO target with a reason, try another eligible configured target when permitted,
or report that no YOLO route is available. An explicit lower-effort tag may override
the preset, but the receipt must show the requested and applied values.

## Selection and account behavior

Proposed usage after implementation:

```text
@yolo solve this difficult problem
@gpt @yolo review this design
@claude @yolo investigate the failure
@grok @yolo research this question
@azure @yolo use my Astra deployment
@max return to my normal maximum tier
```

- Add `YOLO = 4` after the existing tier values, retaining persisted meanings 0–3.
- Require explicit `@yolo` or a user-confirmed `MODEL_REQUEST: YOLO`. Neither the
  classifier nor automatic escalation/risk floors may independently enter YOLO.
- Keep YOLO through continuations of the opted-in task, including interruption
  recovery. A new independent task returns to ordinary classification; an explicit
  tier tag immediately replaces YOLO. Record the source of the opt-in so stale
  session affinity cannot enroll another task.
- The proposed permission to use YOLO is opt-in at configuration level too. Preserve
  existing `policy.max_tier` caps; document that a cap of `max` blocks `@yolo` until
  the operator admits the new tier. A blocked selection must be visible, not labeled
  YOLO while actually executing MAX.
- Choose among eligible subscribed GPT, Claude and Grok accounts using the existing
  account and provider selection machinery. Preserve task preferences within that
  group. Exhaust eligible subscription routes before considering Azure; a lower
  cost or switching penalty must not make Azure jump ahead of that group.
- Rotate eligible profiles within the chosen provider using its existing profile
  rules. Authentication, quota, model access, tool compatibility and context fit
  are separate eligibility checks. Unknown quota is not evidence of exhaustion.
- Explicit provider/account choices remain binding under the existing fallback
  contract. `@azure @yolo` selects Azure directly. Do not silently substitute another
  identity or provider when a pinned request fails.
- Include other backends only when the operator supplies a verified YOLO mapping.
  Existing Gemini, DeepSeek, local-model or API configurations gain no invented
  mapping, authentication or account permission from this proposal.

## Small implementation boundaries

The existing owner is AgentRoute for tier parsing, mappings and provider selection.
The current four-value `Tier` enum and hard-coded tier schemas prevent adding a
fifth tier entirely through configuration. Codex continues to own history,
compaction, native model acceptance and tool permissions.

1. **Tier and configuration compatibility.** Extend `models.py`, the tier tag and
   CLI parsers, policy validation, and configuration schemas. Audit every enum
   iteration and four-tier lookup. Existing configs must load without missing-key
   failures; an omitted YOLO mapping means unavailable, never an implicit MAX
   mapping. Keep ordinary classifier outputs limited to the existing four tiers.
2. **Routing and provider integration.** Resolve the chosen backend's YOLO model
   and effort, preserve task opt-in and explicit pins, apply the subscription-first
   selection groups, and reuse capacity/profile switching. Extend account-specific
   overrides, bridge registration, catalog generation and receipt labels as needed.
   Validate model/provider effort support before execution rather than relying on
   the shared reasoning enum to imply provider support.
3. **User surfaces and release evidence.** Document commands, configuration,
   admission, rollback and failures; update CLI help, diagnostics, route banners,
   model-request parsing and any dashboard tier lists. Keep tests with each logical
   behavior commit. A native Codex change requires its own demonstrated gap and
   the existing pinned-patch validation/release procedure.

Do not bundle authentication cleanup, ordinary MAX remapping, a compaction repair
or a provider update into this feature. Each has a distinct verification and
rollback boundary.

## Acceptance before implementation is ready

| Scenario | Required evidence |
| --- | --- |
| Existing installation | Four-tier config loads and existing selections, overrides and policy caps are unchanged. |
| Explicit selection | All proposed tags parse deterministically; YOLO model and actual effort appear in the execution receipt. |
| Automatic routing | Classifier, risk floors and progress review cannot introduce YOLO without recorded opt-in. |
| Continuation and exit | Continuation preserves YOLO; a new task or explicit lower tier exits it; interrupted recovery retains only the opted-in task. |
| Effort translation | GPT/Azure preserve supported wire effort; Claude `ultra` becomes provider `max`; Grok remains `xhigh`. Unsupported pairs are visibly unavailable. |
| Provider priority | An eligible subscription beats Azure despite score/switching differences; subscription exhaustion reaches Azure only when allowed. |
| Profiles and pins | Eligible profile rotation respects account-specific access and mappings; explicit provider/account choices retain their contract. |
| Missing target or admission | Disabled policy, absent model, unsupported effort, unavailable login, budget block and no fitting context window have explicit outcomes without a false YOLO success. |
| Native context | A provider switch uses that provider/model's verified context window. A long-context smoke can compact and then finish multiple tool steps without repeatedly compacting unchanged context. |
| Live provider smoke | A small real request succeeds through each enabled route and its execution receipt confirms model, provider and effort. Catalog presence or a routing simulation alone is insufficient. |

Run `uv run pytest` and `uv run ruff check .` plus the applicable native checks.
Record live-smoke results separately from mocked routing tests. Only admit model
and effort pairs whose provider smoke succeeds; this proposal itself supplies no
live YOLO execution evidence.

## Rollout and rollback

Ship the feature disabled for existing users. Enable it first on a bounded personal
configuration with explicit mappings, retaining both the active configuration and
any separate launch configuration for rollback. Finish account/catalog validation
and provider smokes before using the new tier for a long task.

To stop using YOLO, select `@max` or another existing tier, disable YOLO admission,
and restore the saved configuration. Before downgrading to a four-tier binary,
remove YOLO configuration values and verify persisted task/receipt readers handle
the new value or keep the compatible binary. Do not erase audit history or account
credentials to make an older binary accept state.

## Source basis and unresolved provider validation

Checked against AgentRoute main at `f2a866b`:

- [`models.py`](../src/agentroute/models.py): four-value tier enum and reason codes.
- [`config.py`](../src/agentroute/config.py): model defaults, policy cap, backend
  targets, profile overrides and provider selection settings.
- [`providers.py`](../src/agentroute/providers.py): Fable 5.2 bridge registration
  and provider/catalog context handling.
- [`router.py`](../src/agentroute/router.py) and
  [`claude_models.py`](../src/agentroute/claude_models.py): Claude effort translation.
- [`grok-bridge.md`](grok-bridge.md): Grok 4.7/xhigh defaults and distinct
  subscription/API routes.
- [`efficiency-and-context.md`](efficiency-and-context.md): native compaction
  ownership and the distinction between configured and verified context limits.

Before implementing the GPT/Azure defaults, confirm the exact accessible Astra
model/deployment and its accepted maximum effort independently on both providers.
The existing names are source evidence, not proof of live entitlement. No additional
Grok tier above 4.7/xhigh is proposed without new provider evidence.
