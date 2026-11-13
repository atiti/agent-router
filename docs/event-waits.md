# Event-driven waits

An agent that is blocked on something outside the session (CI, a deploy, a file another process
will write, a worker, a fixed delay) should stop taking turns until that thing happens. A goal
otherwise continues turn after turn, each one re-reading the context to report the same state.

`agentroute wait` parks a thread and wakes it on an event, on a deadline, or when an outside
script or app says so. No model call happens while the thread is parked.

## Register a wait

Run these from the session that should be woken. The thread comes from `CODEX_THREAD_ID`; pass
`--thread ID` to target another thread.

```sh
agentroute wait pr atiti/agent-router 179                 # all checks settled, merged or closed
agentroute wait pr atiti/agent-router 179 --until merged
agentroute wait pr atiti/agent-router 179 --until change  # any change in state, head, review, checks
agentroute wait run atiti/agent-router 43097645030        # workflow run completed
agentroute wait file build/out.tar                        # --until exists | changed | gone
agentroute wait timer 20m
agentroute wait event import-finished                     # fired from outside, see below
agentroute wait url https://example.com/health --status 200 --contains ok
agentroute wait command --interval 2m -- test -s results.json
```

Every wait takes `--timeout` (default `30m`, or `8h` for named events and packets; at most seven days) and `--note "what to do on wake"`.
When the deadline passes first, the thread is woken anyway with reason `deadline`, so a wait can
never park a thread forever. A thread may hold up to eight pending waits; the first to end wakes it.

If the condition already holds, the command prints `"already_met": true` with the observed state
and registers nothing. A pull request whose workflow is queued without any jobs reports no checks,
which counts as not settled.

Registering a wait pauses the thread's active goal with origin `automatic`. That is what stops
continuation turns. The agent then ends its turn. `wait url` and `wait command` run outside the
sandbox on a schedule, so keep them read-only and expect them to go through approval review.

## What happens on wake

A detached daemon checks conditions (pull requests and runs once a minute, files every five
seconds). When a wait ends it starts one turn on the thread and sets a goal that the wait paused
back to `active`. A goal the user completed, cleared or changed in the meantime is left alone.

The wake message is fixed text carrying only the wait ID. The agent reads the outcome with:

```sh
agentroute wait show w-1a2b3c4d
```

That returns the reason (`condition`, `deadline`, `timer`, `external`), the observed evidence, the
saved note and any payload from an outside trigger. Because these arrive as tool output, a note or
payload can never be mistaken for a message typed by the user or for approval.

Delivery records its intent before starting the turn. If the reply is lost, a visible exact wake marker reconciles admission. Missing history leaves
an unknown hold rather than causing another send. An unloaded
thread is resumed on the shared server first. When the server is down, delivery retries with
backoff. The daemon starts on demand and exits a minute after nothing is pending; any routed
prompt restarts it when waits are outstanding, for example after a reboot.

## Triggers from scripts and other apps

Three ways for something outside the session to cause a turn:

```sh
# 1. Fire an event a session registered with `agentroute wait event NAME`.
agentroute wait fire import-finished --message "412 rows loaded"

# 2. Drop a file. Anything that can write a file can trigger a turn; no AgentRoute call needed.
echo '{"rows": 412}' > ~/.agentroute/state/waits/triggers/import-finished.json

# 3. Start a turn on a thread that registered nothing, from cron, a webhook handler or an app.
agentroute wake --thread 01a1bf89-2c42-7852-8687-e490794cb825 --message-file report.txt
agentroute wake --name "Ivan work packet" --message "nightly sync finished"
```

A trigger for an event name nobody is waiting on yet is kept for one day, so the script and the
session may start in either order. Payloads are limited to 4,000 characters and are delivered as
data through `wait show`.

## Inspect and cancel

```sh
agentroute wait list            # pending waits for this thread
agentroute wait list --all      # every thread, including finished waits from the last week
agentroute wait cancel w-1a2b3c4d
```

Cancelling the last wait that paused a goal reactivates the goal. The routing hook adds a
one-line reminder of pending waits to each prompt on that thread, and on goal continuation turns
it reminds the agent that waiting should be registered instead of polled.

## Limits

- Waits act on threads served by AgentRoute's shared server.
- `wait pr` and `wait run` need a signed-in `gh` on the host.
- State lives in `~/.agentroute/state/waits/`. The daemon log is `daemon.log` in that directory.

## Worker and service handoffs

Wait for the actual prerequisite. Completion of another owner's PR does not establish that a
shared service can restart. Prefer an exact packet condition or a producer's named event:

```sh
agentroute wait packet --namespace private --packet delivery --item worker \
  --until released --step implement --owner worker-session-1 --timeout 8h
agentroute wait packet --namespace private --packet delivery --item worker --until complete
agentroute wait packet --namespace private --packet delivery --item worker --until changed

# The owner or an outside script publishes this generation-specific event after verification.
agentroute wait event service-handoff --event-id release-20261113 --timeout 8h
agentroute wait fire service-handoff --event-id release-20261113 --message-file handoff.json
```

`released` requires the exact done step, an unassigned outcome, the specified last released
owner and no prepared/unknown operations. `complete` requires the canonical completion gates.
`changed` observes execution state, assignment and uncertain-operation presence; ordinary
checkpoint/revision changes do not wake it. These observations grant no effect authority.
The tracker remains the canonical `~/workspace`, with an explicit namespace; missing identities
are observation errors, never readiness.

Named events and packet waits default to an eight-hour fallback deadline. Timers remain useful
for scheduled work. Repeated five-minute checks for a human handoff consume model turns; ask
for the handoff once and park on its event instead. A deadline means reconsider the blocker.

Keyed events are retained for seven days. Retrying the same name, event ID and optional target
with the same payload is a duplicate; a changed payload is rejected. Include the producer's
run/generation in the ID. A new occurrence needs a new ID. Publish before or after registration;
re-registering the same thread/key returns its existing event receipt instead of waking twice.
A deadline or cancelled wait may be rearmed with that same producer key. Add
`--thread ID` to keyed `fire` for an exact target. Legacy unkeyed file triggers retain their
one-day behavior and are intended for simple one-shot signals.

## Verify receipt and follow-up

```sh
agentroute wait show w-1a2b3c4d
agentroute wait resolve w-1a2b3c4d --action waiting --reason "Owner handoff still pending"
agentroute wait stats
agentroute wait stats --all
```

A `show` from the target Codex thread records a recipient-read claim; arbitrary shell inspection
records no receipt. `resolve` records an immutable next-action claim from that target. Actions
are `continue`, `waiting`, `blocked`, `replan`, `complete`, and `duplicate`. This does not
complete the packet or prove useful work. Subsequent effects and validation remain the proof.

The registry stores probe/error/change counters, first/last observation times, the last error
after recovery, and at most twelve observation transitions. `stats` distinguishes admission
into a new idle turn from an existing or queued active turn when the pre-admission status is
available. Status can race, so these modes are observations, not atomic native receipts.
Legacy records lack these counters. No token savings are inferred from parked time.

After a transport timeout, a visible exact wake marker can reconcile admission. Absence from
history cannot prove rejection: it leaves `unknown_hold`, with no automatic resend. Inspect
the original session to resolve that hold; do not issue a new wake as a workaround.

A wake or cancellation only resumes the exact goal snapshot and unchanged durable mutation
suffix paused by that wait, and leaves
other outstanding waits parked. A changed goal or later user pause remains untouched. Legacy
waits without a pause snapshot and mutation guard do not automatically resume a goal. If a user resumes early,
the routing reminder permits independently runnable work and says to retain pending waits
when the remaining prerequisite is unchanged.
