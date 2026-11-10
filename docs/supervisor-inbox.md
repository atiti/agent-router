# Durable supervisor coordination

Supervisor messages are persisted in the Herdr team ledger before terminal lookup
or any delivery. Supervisor messages never submit pane prompts or user-style
turns, so a paused or budget-limited native session cannot be restarted through
transport. `queued`, legacy `rejected`, and `unknown` receipts never trigger
automatic retries. Worker-directed transport keeps its existing behavior. Repeating an
identical message ID repairs the projection and returns its original receipt.
A different body, recipient, or actionable flag requires a new ID.

First verify the installed native executable/build and run its consumer canary;
then an operator enrolls the exact team/thread binding with
`inbox-enable TEAM --consumer-version 1 --evidence INSTALLED_BUILD_AND_CANARY_RECEIPT`.
The evidence is an operator-validated receipt, not automatic capability proof from
a nonempty string. Repeating identical enrollment is idempotent; different binding
or evidence is rejected. Unenrolled supervisor messages stay durably queued with
`delivery_hold: native_inbox_enrollment_required`; they never fall back to prompts.
Existing active teams must be enrolled only through a coordinated runtime handoff.

The single Python writer atomically publishes an owner-only snapshot at
`$CODEX_HOME/supervisor-inbox/<registered-thread-UUID>.json`. It holds the team
lock and the projection lock, fsyncs before rename, and fsyncs the directory.
The native harness reads this file at safe boundaries, independently of the
pane prompt. It never writes the team ledger. Native consumption is separate
from explicit delivery, acknowledgment, and disposition.

A snapshot has `version: 1`, `role: "supervisor"`, `thread_id`, `team`, `member`,
`namespace`, `packet`, `item`, `objective`, `generation`, `live_children`, `events`,
and `overflow`. Only a registered supervisor or a verified native anchor can
publish a supervisor snapshot. Anchor migration uses the existing live Herdr
client registry. A report cannot confer a routing role or action authority.

Each event has `id`, `state` (`persisted`, `delivered`, `acked`, `dispositioned`),
`actionable`, `kind`, `message`, `source: {team, member}`, `created_at`, and
`delivery: {state}` (`pending`, `attempting`, `delivered`, `unknown`). The event
state records supervisor handling; delivery records native delivery readiness and
legacy transport reconciliation. New supervisor messages remain pending until
explicit native consumption; no pane transport is attempted.
Legacy delivered notifications leave the event persisted until explicit
consumption. Acknowledgment alone leaves coordination actionable.

From the addressed supervisor's actual Codex session:

```sh
agentroute herdr team inbox TEAM --member SUPERVISOR
agentroute herdr team inbox-ack TEAM EVENT_ID --member SUPERVISOR
agentroute herdr team disposition TEAM EVENT_ID --member SUPERVISOR \
  --action owner_handoff --reason 'Exact overlap resolved' \
  --owner EXISTING_WORKER --next-action 'Existing worker finishes its claimed files' \
  --evidence ACCEPTED_HANDOVER_RECEIPT
```

`continue`, `replan`, and `owner_handoff` require the accountable owner and exact
next action. A handoff declaration is a recorded decision; its evidence must
reference a confirmed handover/continuing owner receipt. It does not transfer an
active claim or prove a worker stopped. `resolved` and `duplicate` require a
reason and evidence. The caller remains responsible for validating those sources.
No disposition itself grants permission to deploy, send, spend, or take over.

`awaiting_user` and `waiting_external` additionally require `--question-ref` and
`--resume-condition`. Their active `wait` records have `owner: user|external`,
`delivery_state: delivered`, `question_ref`, and `resume_condition`; the question
must actually have been delivered through its authorized channel. A valid wait
stays visible after disposition, suppresses automatic continuation, and prevents
completion. After observing the response, use `inbox-resume TEAM EVENT_ID --member
SUPERVISOR --evidence RESPONSE_RECEIPT`; it removes the active wait and preserves
it in `wait_resolution` and creates one stable actionable `wait_resolved` event
with `resolves_event` linking the original wait. The supervisor must disposition
that next decision even without an active native goal. Repeated resume evidence
does not create another event. It does not approve the underlying action.

`send --no-actionable` records an informational report without creating native
coordination debt. All ordinary supervisor reports default to actionable because
only the sender can decide whether the report needs a decision. The monitor uses
stable persisted occurrence IDs and enqueues changed health sets through blocked
panes and notification cooldowns. A fresh full poll dispositions superseded health
sets only after the new set is durable; the evidence identifies the poll/digest.

Child entries have `id`, `state` (`running`, `queued`, `waiting`, `stopped`,
`complete`, `unknown`), `observed_at`, `source`, `reconcile_required`, and
`observation_ttl_seconds: 120`. The monitor records actual terminal observations;
missing/stale observations become unknown and require reconciliation. A stopped
terminal is not a completed outcome. Only verified canonical completion/cleanup
produces complete. Native readers must also expire old observations if the writer
stops. Timestamp-only observation refreshes retain generation to avoid model churn.

The native projection is at most 262144 bytes, 128 events, and 128 children.
It includes unresolved actionable events and active waits. Dispositioned history
and informational reports remain in the complete team ledger (`team messages`).
Messages are UTF-8 summaries up to 1024 bytes; objective up to 8192 bytes.
Actionable events precede active delivered waits. `overflow.events` counts
omitted actionable events, `overflow.children` counts unresolved children; both
require reconciliation and prevent completion. `overflow.waiting_events` counts
omitted valid delivered user/external waits; these prevent completion quietly
without creating repeated inference. Archived children are retained only in history. Dispositioning a page exposes the next page;
no pending event is dropped. The native notice directs the model to the complete
ledger when a summary is insufficient. These bounds do not limit durable history.

The monitor also reads canonical `next --role coordinator`, filters the exact
packet, and verifies each candidate with the v1 `execution` contract. Unassigned
claimable steps and explicitly coordination-required assigned items produce stable
`packet_step` events with full execution receipts. Ordinary assigned active parent
work does not create its own pending obligation. Canonical candidates remain
queued children with `source: canonical-scheduler` and `reconcile_required: true`
until a fresh scheduler read confirms an actual claim or readiness change. Recording
a disposition does not prove dispatch and cannot close this obligation. Readiness
grants no action authority; later outcome holds remain in canonical state.
For explicitly structured `waiting_external`/`waiting_worker` step or outcome
waits, a timezone-aware canonical `item.next_review` produces one durable
`deadline_review` event when due. It is stable per item/deadline and never infers
waits from legacy blocked prose. Future reviews, user waits, and terminal items
remain quiet. The reminder directs inspection of the existing exact watch/source
and recorded resume condition, preserving all effect/approval holds; it never
starts another watch, job, retry or external action. A changed/resolved canonical
wait or deadline supersedes the old review; the owner records the next review
through the existing packet lifecycle.

Eligibility epochs advance only when a previously absent step becomes a candidate
again. Event and queued-child identities include that epoch, so a consumer that
misses the claim/release interval still observes a fresh decision instead of an
old disposition; repeated unchanged polls remain deduplicated.

Registered supervisors retain a minimum automatic SMART route and their ongoing
objective during short coordination turns. Explicit user routes and policy caps
remain authoritative. Bounded workers retain their existing automatic routing.


## Manual admission recovery

An uncertain native admission is held across restart. A generic continue, an
inbox message, or an authority string does not reconcile it. Only an operator
with actual approval for that exact admission may invoke:

```sh
agentroute herdr team admission-reconcile TEAM --member SUPERVISOR \
  --reservation TOKEN --decision not_admitted \
  --evidence VERIFIED_NO_ADMISSION_RECEIPT --authority EXACT_USER_DECISION_REF
```

Decisions are `not_admitted`, `completed`, or `interrupted`. Not-admitted needs
actual evidence that no admission occurred. Completed requires the native
consumer's own known terminal history. Interrupted holds/suspends pending effects;
it does not authorize their retry. The tool/approval boundary must validate the
real operator decision: CLI execution and a nonempty authority citation are not
permission proof. This command must never be invoked automatically from report
prose. Source implementation authority alone does not approve production recovery.

The recipient thread comes from the existing team ledger. Before creating a
record, the writer reads the owner-private regular native cursor at
`supervisor-inbox-cursors/<thread>.json` (0600 file, 0700 directory, at most
1024 bytes) and requires the exact current unresolved reservation/admission token.
An unrelated, future, or already settled debt is rejected. Native validation still
checks token and terminal history; the writer never decides those outcomes.
The writer atomically
creates an immutable owner-only v1 record at
`$CODEX_HOME/supervisor-admission-resolutions/<thread>/<token>.json` with `version`,
`thread_id`, `reservation`, `decision`, `evidence`, `authority`, and `created_at`.
Tokens match `[A-Za-z0-9_-]{1,160}`. Evidence has at most 32 nonempty references,
up to 4096 characters each; authority has the same character bound. The complete
serialized resolution must also fit 8192 bytes. Repeating
identical data returns the existing record; changed decision, evidence, or
authority is rejected. The CLI never edits the Rust cursor. A record settles only
that exact admission after native validation and grants no external-effect retry.
