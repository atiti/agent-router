# YOLO tier: an explicit fifth tier above MAX

Status: implemented and disabled by default. `@yolo` is blocked until an operator sets
`policy.max_tier: yolo`. Mocked routing tests cover the offline acceptance rows below;
live provider smokes are still required before relying on any route (see the last rows).

## Outcome

An opt-in fifth tier for work where the user deliberately chooses the strongest available
model and its highest supported reasoning effort. Ordinary routing stays within FAST,
NORMAL, SMART and MAX. YOLO selects a model and effort; it does not change tool
permissions, approvals, sandboxing, budgets or account access.

MAX keeps each installation's existing mapping (for example GPT-6 Astra at `xhigh` or
`high`); YOLO uses Astra's maximum supported effort. Existing MAX mappings are never
rewritten.

## Model and effort choices

| Backend | YOLO target | Router effort | Provider effort | Notes |
| --- | --- | --- | --- | --- |
| ChatGPT (`gpt`) | The exact Astra model the configured MAX names (`gpt-6.2-astra` in new installs, `gpt-6-astra` on some accounts); unmapped if MAX is not Astra | `ultra` | `ultra` | `ultra` is the existing Codex spelling for the highest GPT effort (`ReasoningEffortConfig::Ultra` in the pinned runtime patch). Account entitlement is checked through the existing account catalog. |
| Claude subscription (`claude`, explicit `fable`) | `claude-fable-5-2` | `ultra` | `max` | The router represents Claude `max` as `ultra`; `claude_effort()` and the bridge translate it back to `max`. |
| Grok subscription (`grok`) | `grok-4.7` | `xhigh` | `xhigh` | Intentionally equal to Grok MAX. No stronger Grok effort is known; `ultra`/`max` are rejected. |
| Azure (`azure`) | The exact Astra deployment the configured Azure MAX names (for example `dev-gpt-6-astra`); unmapped if MAX is not an Astra deployment (the bundled `gpt-5` default is not) | `ultra` | `ultra` | Deployment availability and accepted effort are independent of ChatGPT access. Azure is the last automatic fallback, and only when it is an admitted candidate. |

These bundled targets are recommendations, not quality benchmarks. They are merged in
memory when configuration loads (or a bridge registers), only where no `tiers.yolo` entry
exists, and only for backends whose Codex provider identity matches the bundled backend (`openai`, `agentroute-azure`,
`agentroute-claude`, `agentroute-claude-fable`, `agentroute-grok`). Gemini, DeepSeek,
`grok-api`, local and custom backends receive no mapping: an omitted mapping means that
backend cannot serve YOLO, never a fallback to its MAX target.

**On disk.** Bundled targets are never written by `save_config`: a saved file keeps only
YOLO entries the operator configured or changed, so files stay readable by releases
without YOLO (until the operator writes `max_tier: yolo` or a `tiers.yolo` entry). The
GPT backend's merged target no longer leaks into `providers.codex.tiers` for legacy
configurations. `policy.max_tier` normalizes known tiers, including `yolo`, and keeps
unknown values as before.

Because bundled targets are not stored in the file, deleting a `tiers.yolo` entry does
**not** disable one: the merge-on-load step adds the bundled target again. To keep YOLO off
a bundled backend, remove it from `routing.provider_selection.candidates` (unpinned YOLO
then skips it), override its `tiers.yolo` with a different verified target, disable the
whole backend (`enabled: false`), or keep YOLO off everywhere with `policy.max_tier: max`
(the default).

Each target's effort, and then the final applied effort, is validated per provider family
(GPT/Azure: `none`…`ultra`; Claude: `low`…`max`/`ultra` on effort-capable models; Grok:
`low`…`xhigh`; other backends: operator-verified, not validated) and per model where
capability data exists (a model declared with `reasoning: false` accepts no effort; there
is no per-effort model data, so `ultra` is not restricted further). An unsupported pair is
unavailable with a reason such as `unsupported_effort:grok-4.7/ultra`; this includes an
explicit `@grok @yolo @ultra`, which stops the turn instead of being sent. Effort is never
silently lowered. An explicit effort tag (`@yolo @high`) may override the preset; the
receipt's `yolo` block records the preset, requested, explicit and applied efforts.

**Effort on continuation.** A YOLO continuation carries an effort only when it was an
explicit user effort (`explicit_reasoning_effort` in the previous receipt) and the backend
is unchanged. Otherwise the current backend's YOLO preset applies: a continuation that moves
from Grok (`xhigh`) to GPT runs Astra at `ultra`, and one that moves from GPT (`ultra`) to
Grok runs `xhigh`.

## Selection and account behavior

```text
@yolo solve this difficult problem
@gpt @yolo review this design
@claude @yolo investigate the failure
@grok @yolo research this question
@azure @yolo use my Astra deployment
@max return to my normal maximum tier
```

- `Tier.YOLO = 4` follows the existing values; persisted meanings 0–3 are unchanged.
  Audit rows store the tier name `yolo`.
- YOLO requires an explicit `@yolo` or a user-confirmed `MODEL_REQUEST: YOLO` (offered to
  agents only while YOLO is admitted). Classifier outputs are clamped to MAX; risk floors
  cannot be configured as `yolo`; hysteresis, cache retention, task stickiness and progress
  review see a YOLO session as MAX; a final router invariant clamps any automatic YOLO to
  MAX (`YOLO_CLAMPED`).
- Continuations of the opted-in task, including interruption and goal recovery, keep YOLO
  only when the previous route's receipt recorded an opt-in source (`explicit_tag`,
  `agent_request`, `continuation`, `interrupted_turn`). A model shared with MAX never
  implies YOLO. A new independent task returns to ordinary classification; `@max`, any
  other tier tag or `@auto` exits YOLO.
- Admission reuses `policy.max_tier`. The default `max` blocks `@yolo` with a visible stop
  message (`YOLO_BLOCKED`, trigger `yolo_admission`); it never runs MAX labeled YOLO.
- A blocked attempt (not admitted, no eligible route, unavailable pin, missing mapping or
  unsupported effort) stays visible: the audit row keeps tier `yolo` with
  `capacity_status: yolo_blocked`, and the receipt's `yolo` block has `status: blocked`,
  the reason and `attempted_opt_in`, but `opt_in_source: null`. The hook ignores such a
  row for continuation (previous tier and effort), so the next plain prompt such as
  `continue` is routed by ordinary FAST–MAX classification. Only a new explicit `@yolo`
  hits the block again. `yolo_blocked` also avoids the capacity recovery hold that an
  ordinary `blocked` status would trigger.
- Unpinned YOLO ranks only `routing.provider_selection.candidates` (the existing admission
  boundary), in every routing mode: eligible GPT, Claude and Grok subscriptions first
  (ordered within the group by task preference, usage and switching scores), then other
  admitted candidates that have a YOLO mapping, and Azure last only when `azure` is in the
  allowlist (it is not by default). A backend outside the allowlist is reachable only
  through an explicit pin such as `@azure @yolo`. Scores never move Azure ahead of an eligible subscription. Capacity
  fallback continues down the same list.
- ChatGPT accounts rotate under the existing account rules; an account's `tiers.yolo`
  override is honoured and validated. Authentication, quota, model access, tool
  compatibility and context fit remain separate eligibility checks. Unknown quota is not
  evidence of exhaustion.
- Explicit provider, session and interrupted-turn pins are binding. `@azure @yolo` selects
  Azure directly; if a pinned backend is unavailable or lacks a valid YOLO target, the turn
  stops (`yolo_unavailable`) instead of moving to another provider or identity.

## Implementation boundaries

AgentRoute owns tier parsing, mappings and provider selection; Codex continues to own
history, compaction, native model acceptance and tool permissions. No native Codex patch is
part of this change.

1. **Tier and configuration compatibility** — `models.py` (`Tier.YOLO`, `ORDINARY_TIERS`,
   `Tier.automatic()`), `config.py` (bundled targets, non-overwriting merge, `max_tier` and
   risk-floor validation, `TierUnavailableError`), tag and `MODEL_REQUEST` parsing,
   classifier clamps, progress review and analytics tier lists.
2. **Routing and provider integration** — `yolo.py` (target resolution, effort support,
   priority groups), `provider_selection.py`, `router.py` (opt-in tracking, admission,
   binding pins, receipts) and `hook.py` (recorded continuation, account targets).
3. **User surfaces** — README, `agentroute help`, route banner (`YOLO OPT-IN (source)`,
   `YOLO NOT RUN`, `YOLO CLAMPED TO MAX`), stop message, `agentroute doctor` (`yolo`
   check), CLI validation (`backend-route` rejects `yolo`; `backend-add` reserves it).

## Acceptance

| Scenario | Required evidence | Status |
| --- | --- | --- |
| Existing installation | Four-tier config loads and existing selections, overrides and policy caps are unchanged. | Tested (`tests/test_yolo_tier.py`). |
| Explicit selection | All tags parse deterministically; YOLO model and actual effort appear in the execution receipt. | Tested (parsing, selection receipt, hook output). The Codex execution receipt needs a live smoke. |
| Automatic routing | Classifier, risk floors and progress review cannot introduce YOLO without recorded opt-in. | Tested. |
| Continuation and exit | Continuation preserves YOLO; a new task or explicit lower tier exits it; interrupted recovery retains only the opted-in task. | Tested (router and hook). |
| Effort translation | GPT/Azure preserve supported wire effort; Claude `ultra` becomes provider `max`; Grok remains `xhigh`. Unsupported pairs are visibly unavailable. | Tested (routing). Provider acceptance needs a live smoke. |
| Provider priority | An eligible subscription beats Azure despite score/switching differences; subscription exhaustion reaches Azure only when allowed. | Tested (`tests/test_yolo_routing.py`). |
| Profiles and pins | Eligible profile rotation respects account-specific access and mappings; explicit provider/account choices retain their contract. | Tested for account overrides, unsupported account targets and provider/session pins. Multi-account rotation reuses existing tested machinery. |
| Missing target or admission | Disabled policy, absent model, unsupported effort, unavailable login, budget block and no fitting context window have explicit outcomes without a false YOLO success. | Tested for policy, missing mapping, unsupported effort, unavailable pinned backend and no eligible route. Login, budget and context-window exclusions reuse existing provider-selection checks. |
| Native context | A provider switch uses that provider/model's verified context window. A long-context smoke can compact and then finish multiple tool steps without repeatedly compacting unchanged context. | Needs live smoke. |
| Live provider smoke | A small real request succeeds through each enabled route and its execution receipt confirms model, provider and effort. | Not done; catalog presence or routing simulation is insufficient. |

Run `uv run pytest` and `uv run ruff check .`. Record live-smoke results separately from
mocked routing tests, and admit only model and effort pairs whose provider smoke succeeds.

## Rollout and rollback

Ship disabled. Enable it first on a bounded personal configuration with explicit mappings,
keeping both the active configuration and any separate launch configuration for rollback.
Validate account catalogs and run provider smokes before using YOLO for a long task.

To stop using YOLO, select `@max` or another tier, set `policy.max_tier: max`, and restore
the saved configuration. Before downgrading to a release without YOLO, remove `tiers.yolo`
entries and `max_tier: yolo`. Older releases cannot parse the `yolo` audit tier, so their
hook fails open in a session whose latest route was YOLO; start a new session there. Do not
erase audit history or account credentials to make an older binary accept state.

## Unresolved provider validation

Confirm the exact accessible Astra model/deployment and its accepted maximum effort
independently on ChatGPT and Azure before admitting YOLO. The bundled names are source
evidence, not proof of live entitlement. No Grok tier above 4.7/xhigh is configured without
new provider evidence.
