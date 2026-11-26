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

The default **Focus** view opens on **Needs you**. Use the counts across the top to see
items that need an owner or a handoff, wait on an agent or outside result, or are in progress.
Each card shows the latest recorded progress, what is missing, what happens next, and who
is responsible. Overdue reviews are listed first. Use **Review items** or a card’s **Details**
button to step through the current list, and **Review decisions** to work through proposals.

The action on each card opens the relevant review or prefills a reply you can edit.
**Request triage** asks an agent to identify an owner. **Ask for follow-up** checks a handoff.
**Ask for an update** goes through the packet’s reply route. **Ask for a dependency check** asks the
recipient to verify the outside result and record who will follow up. Focus shows the actual
recorded blocker and next step when they are available. Use **Recent dashboard activity**
to see what was delivered and how the packet stands.

**Request triage** stays open until the receiving session records a structured result in
the packet. The agent links the next step to the originating wait with
`next_step.triage_ref = "agentroute:wait/WAIT_ID"` and records either a current claim with
an executable step, a named handoff with an event reference, or a delivered question.
It then runs `agentroute wait resolve WAIT_ID --action ACTION --triage-revision REVISION`.
Focus shows a pending handoff under the responsible agent or coordinator. The supervisor
verifies pickup before recording acceptance. The explanatory reply stays in the receipt.

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

In Focus, **Review decisions** steps through requests you can act on. Open an individual card
for earlier decisions, comments or a request that needs to be handled in its session. Review the
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

For worker handoffs, Focus shows current supervision and the review receipt when one is
available. The team monitor forwards due reviews and flags an undated worker handoff once.
The supervisor checks the existing worker and records an event or next review date.

The native inbox also shows an assigned executable step that has gone 30 minutes without
an owner checkpoint. The team supervisor verifies the worker's current activity and resumes
eligible work with that owner. The follow-up keeps the existing claim and is deduplicated
until the step, review date, or owner checkpoint changes.

Run `agentroute maintenance run` for a current inventory of unfinished outcomes, recorded
owners, conversation bindings, supervisor monitoring, and outstanding feedback. It reports
the step and outcome questions separately. To ask for feedback, record a delivered question,
its decision maker, and what answer will let the work resume. Use **Reply** for factual input
and **Review decisions** for a scoped approval. The packet owner records the answer and next
step. Ordinary conversations outside an enrolled team still need operator follow-up.

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

## Voice on the Mac

Run `agentroute voice` in a terminal on the Mac running the shared App Server.
Open the printed local link in your Mac browser, click **Start talking**, and
allow microphone access. Use your usual headphones or speakers and speak
normally when connected. For push-to-talk, select **Push to talk** before
connecting, then hold the button or hold **Space** while the button is focused.
Pressing it silences the speaker immediately and opens the microphone.
Releasing it closes the mic so you can listen without transmitting room speech.
Keep the tab open while you listen. Click **End conversation** or close the tab
to stop.

Ask “What needs me?”, “What is the release session working on?”, or “Tell the
release session to verify the checks and report back.” The conversation reads
current session evidence on demand. While you listen, it can also announce new
blockers and completions. Say “only blockers” or “quiet updates” to change that
for the current conversation.

Commands address an exact session. A reply follows the existing AgentRoute
queue and has a receipt. Steering and interrupts use the exact active turn on
the shared App Server. Codex approval prompts use their existing controls.
The dedicated conversation uses
the GPT route and the OpenAI voice call. Sign in with a ChatGPT account with
Codex voice access. The pilot uses the shared owner already running on the Mac.

Voice conversations are saved as timestamped JSONL files in `~/.agentroute/voice-transcripts/`. Each line contains the time, speaker, and completed transcript text. The terminal prints the file path when a conversation starts. The directory and files are private to your account.

Voice can send a message to a shared App Server session or a registered Herdr
team member by name. Shared turns also support steering and interruption by
turn ID. Herdr supervisor messages go through the team's native inbox. Ask
Voice to check the receipt if you want to know whether a message was queued,
entered in the terminal, accepted by Codex, or acknowledged. The voice page
shows the latest receipt, the exact target conversation, and any verified reply.
A reply is linked only when the target session includes the instruction's
receipt marker in a final answer. Some replies may arrive without that marker.

Say “be more conversational,” “keep answers brief,” “only announce blockers,”
or “give updates less often” to change saved preferences. New voice calls
carry forward a bounded excerpt of recent local transcripts. The live feed
groups fresh reports by project and surfaces verified state changes. Ask
“Where did that update come from?” for the reporting session and observation time.
