# Live dashboard

`agentroute dashboard` serves a page that shows what every agent on this machine is doing right
now: which ones are working, parked on a wait, blocked or finished, what each one is touching,
and what it costs.

```sh
agentroute dashboard                 # opens http://127.0.0.1:8719/
agentroute dashboard --no-open       # print the address only
agentroute dashboard --port 0        # any free port
```

The server runs until you stop it with Ctrl+C. The page updates every two seconds without a
reload.

The default **Focus** view puts live requests and recorded decisions for you first.
Each held outcome shows the recorded reason, owner, and condition for resuming.
Older blockers without a named owner appear under **Blocked with no owner**.
The claims section shows whether a namespace has reached its worker limit.
Use `agentroute dashboard --me NAME` to match a different name recorded in the tracker.

## What it shows

The tiles at the top count agents by state. Anything that needs a person appears in a strip
below them: a pane asking for input, a turn that has gone quiet, a blocked goal, or a wait
that is past its deadline.

| State | Meaning |
| --- | --- |
| Needs you | Herdr reports the pane is waiting for input or approval. |
| Working | A turn is in progress. |
| Waiting | No turn is running and the thread is parked on `agentroute wait`, or its task is waiting. |
| Stalled | A turn is open but nothing has happened for 15 minutes. |
| Blocked | The goal is blocked, or stopped by a usage or budget limit. |
| Idle | Nothing running and nothing pending. |
| Done | The goal or task is complete, or a one-shot run finished. |

The tabs give five views of the same data:

- **Board** lists agents in a column per state. Each card shows the directory and branch, the
  routed model, the current tool call, the goal, tokens used in the period and in total, and
  how full the context window is.
- **Map** draws agents and what they reach: directories, GitHub repositories and pull requests,
  hosts, services, websites, MCP servers and work packets. A target outlined in the text
  colour is shared by more than one agent. Scroll to zoom, drag to pan, click a node to trace
  its links.
- **Waits** lists pending waits with their last check and deadline, then recent results.
- **Work packets** lists unfinished outcomes with their next action, criteria met, delivery
  gates and the session that holds each one.
- **Usage** charts tokens by model over the period and ranks agents by use.

Click any agent for its detail: goal and completion review, task summary, last message, tool
counts, recent tool calls with their commands, routed turns with tokens and outcome, and wait
history. The filter box narrows every view by title, directory, model or tool, and the period
selector changes the window from one hour to seven days.

Supervisor workers appear as their own cards under the name the supervisor gave them.
Progress and approval reviewers are counted in one tile, since there are hundreds a day.

## Reply to a work packet

Use the tab opened by `agentroute dashboard` or the reply link printed with `--no-open`.
Open **Reply** on an outcome in Focus or Work packets. Pick a template (Continue, Hold, or
Ask for details) and edit the message, or write your own. The dashboard uses the packet's assigned Codex thread or a verified binding for an older
worker label. **Open in Herdr** focuses its existing pane or reopens the same conversation
if its view was closed. Sending a reply also tries to show the recipient in Herdr.

If there is no verified session, the dashboard opens and reuses a dedicated review conversation
for that outcome. The review conversation can assess what can move next and preserves any
existing claim. Conflicting known bindings need reconciliation before the dashboard can route
a reply. For a Herdr Core worker, use **Open in Herdr** and reply in its terminal. Its owner
is separate from the shared server used for browser replies.

The reply queues a wake for the selected session through AgentRoute. The session can recheck
the blocker and continue permitted work using the packet tracker. Keep any required approvals
in their normal approval flow. Replies alone do not clear a hold or transfer ownership.

Reopen the reply dialog to see recent delivery receipts. **Delivered** means the shared server
or supervisor inbox accepted the wake. **Read by agent** means the recipient loaded the record.
The receipt shows the Herdr pane when it was verified. An agent response records its next action. Check the packet itself for evidence that work moved.

## Review decisions

In Focus, **Review decisions** steps through work packet questions addressed to you. Review the
recorded proposal and revision, then choose **Approve**, **Deny**, or **Comment**. Approve and
Deny require a macOS owner confirmation that displays the exact proposal. The confirmed choice
goes to the packet conversation as a user decision. The packet owner still verifies the scope
and records the outcome. Use Comment for a coordination message.

A Codex reference can supply the proposal when its exact completed turn contains one
unambiguous question. Otherwise, review the original in Herdr or record explicit proposal
text in the packet's waiting step (`wait.proposal`) before approving. Older dated questions
with full inline proposals also work. For live Codex tool permissions or Herdr Core owners,
use the native session. If delivery is unconfirmed, inspect the session before taking further
action. A retry with the same reference cannot send another turn.

## Where the data comes from

The server reads state other components already write. Replies add a record to the local wait
registry. Packet state is updated by its owner through the tracker:

| Source | Used for |
| --- | --- |
| `$CODEX_HOME/state_5.sqlite` | Threads, titles, task status and summaries |
| `$CODEX_HOME/goals_1.sqlite` | Goals and completion reviews |
| Each thread's rollout file | Open turn, tool calls, targets, last message, context use |
| `$AGENTROUTE_HOME/audit.db` | Routed model, tier and tokens per turn |
| `agentroute wait` registry | Pending and recent waits |
| `herdr agent list` | Pane, workspace and "waiting for you" status |
| `~/workspace/tasks/packets` | Work packets (`WORK_PACKETS_ROOT` overrides the root) |

A missing source removes its part of the page and nothing else.

A rollout is read from its last 1.5 MB on first sight and incrementally after that, so tool
counts for a long session cover its recent part. The detail view says when that applies.
Token totals count each turn once, even when the audit holds several rows for it.

## Access

The server listens on `127.0.0.1` and answers only requests whose `Host` header is a loopback
name or the address given with `--host`, so a web page on another origin cannot read it. It has
no login. Commands are shown as the agent ran them, with values that look like credentials
masked; treat the page like your terminal scrollback. To view it from another device, forward
the port over SSH rather than listening on a network address.

The reply link is private to the running dashboard and authorizes local replies. A plain
dashboard URL or a network bind serves the read-only views.
