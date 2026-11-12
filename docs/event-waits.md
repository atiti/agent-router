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
agentroute wait pr atiti/agent-router#179                 # all checks settled, merged or closed
agentroute wait pr atiti/agent-router#179 --until merged
agentroute wait pr atiti/agent-router#179 --until change  # any change in state, head, review, checks
agentroute wait run atiti/agent-router 43097645030        # workflow run completed
agentroute wait file build/out.tar                        # --until exists | changed | gone
agentroute wait timer 20m
agentroute wait event import-finished                     # fired from outside, see below
agentroute wait url https://example.com/health --status 200 --contains ok
agentroute wait command --interval 2m -- test -s results.json
```

Every wait takes `--timeout` (default `30m`, at most seven days) and `--note "what to do on wake"`.
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

Delivery records its intent before starting the turn. If the reply is lost, the daemon looks for
the wake marker in the thread before sending again, so a wake is not delivered twice. An unloaded
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
