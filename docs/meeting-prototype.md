# Meeting media prototype

This prototype lets a hosted meeting participant supply microphone audio to
AgentRoute Voice and play its WebRTC response, bound to **one prepared native
worker thread**. It does not create a meeting bot, move credentials, configure
public ingress, download a recording or establish live Meet acceptance.

Use an existing server's shared AgentRoute owner and a prepared Aura work session.
The worker's identity, approved tools and permissions remain authoritative. The
voice coordinator retains the existing explicit GPT/ChatGPT voice route. Linux
voice entitlement and media reachability must be qualified on the actual server.

## Native capability boundary

App Server `dynamicTools` adds tools; it does not replace normal native tools.
The coordinator therefore selects no execution environments/workspace roots,
disables every effective MCP server by name, and disables apps/plugins/browser,
agent delegation, host skills and memory instructions for its own thread. Work
sessions retain their capabilities. Failed effective-config reads reject admission.

A disposable native Codex 0.172.0 probe used isolated state and a loopback fake
model endpoint on the GPT-6.1 Sol and GPT-6 Astra catalogs. Some catalog-provided
helpers remain advertised. Actual hostile calls were rejected: `apply_patch`
could reach no execution environment, MCP listing returned no resources and
reading a disabled MCP server failed. The allowed dynamic tool still returned
its fixture result. Tool visibility alone was not treated as an escape or as a
proof of safety. No real model or business action was called.

The behavior must be requalified with the pinned native stack when its tool or
config contract changes. The optional native regression test uses only the
provided test binary and local fixtures; no real account is required. This does
not establish arbitrary model/extension compatibility or production admission.

## Run on the chosen server

Prepare the exact worker session in the same shared owner. Give it only the
meeting's permitted sources and effects. Run this checkout without replacing an
installed runtime:

```sh
uv run python -m agentroute.voice_meeting \
  --meeting-id aura-demo-01 \
  --thread-id 00000000-0000-4000-8000-000000000001 \
  --access-key-file /private/operator/meeting-access.key \
  --port 8875 --https-origin https://meeting.example.com
```

Replace the sample UUID, key path and origin with actual approved values. The
key file's parent must exist. The process creates an owner-only key file if
missing; the token is never printed. The listener remains on loopback. A reviewed
existing HTTPS proxy must forward the exact Host and Origin without exposing any
other AgentRoute route. There is no automatic tunnel or proxy configuration.

Supply the participant page as `https://meeting.example.com/#access=<scoped-key>`
through the bot provider's protected configuration. The page removes the fragment
from its address and does not persist it in browser storage. The provider can
read this credential: it authorizes this meeting connection only. Never use the
private operator Voice key or native App Server credential here.

For Recall, the documented shape is `output_media.camera.kind: webpage` and
`output_media.camera.config.url: <private participant URL>`. The hosted page
receives meeting audio with `getUserMedia`, starts without a click, plays the
remote audio track and sends a heartbeat every ten seconds. No bot creation is
performed by this module. Its status tile is sufficient video for this prototype.

## Scope and work

The tested MeetingPilot boundary defaults to observation: only the prepared
worker's status/results are exposed. `--allow-requests` selects `message_session` for participant
requests. It does not grant business-effect authority or verify a speaker. Use a
worker with safe reads/drafts and existing operator approval for consequential
actions. Mixed audio cannot establish that a named person approved a send.

Inventory, detail, progress and announcements exclude unrelated sessions. Guessed
thread and receipt IDs are rejected. Meetings do not load the private operator's
prior dialogue or alter shared preferences. Steering and interrupting native work
are not exposed in this first version. Human speech still interrupts voice through
the native realtime media pipeline; stopping media does not interrupt the worker.

## Admission and recovery

Before native start, the endpoint writes a private exclusive admission
record under `meeting-admissions/<meeting-id>.json`. A duplicate start, process
restart or uncertain response holds that same meeting with HTTP 409. The prototype
does not automatically reconnect into a fresh voice thread, because fresh tool
IDs could replay accepted work. Inspect the original session and worker receipts
before deliberately admitting a new meeting attempt. Do not delete the admission
record or change the meeting ID merely to retry an unknown effect.

Voice transcripts remain under `meeting-transcripts/<meeting-id>/`. The page does
not display task contents, receipts, paths or session IDs in the video tile.
Keep the private server records with the prototype evidence. A full recording,
retention policy and import/read path must be added and tested separately.

The reused Voice lease closes abandoned media after 90 seconds without heartbeat.
The server supports one admitted connection. Its request key is a meeting-level
credential; individual attendee identity is not established. Connection failures
end media and leave task state for reconciliation. There is no transparent
failover or provider retry daemon in this slice.

## Verification

`uv run pytest tests/test_voice.py tests/test_voice_meeting.py tests/test_voice_ui.py`
checks source isolation, exact target/receipt confinement, disabled actions,
authenticated HTTP admission, replay holds and existing voice behavior. These
tests use disposable fixtures and do not call a provider or a live agent.

The actual acceptance requires an authorized controlled Meet running from the
server while the operator's device is offline: audible response, interruption,
a second question during real worker activity, exact permitted provider result,
recording retrieval and work retention after meeting exit. Record each outcome
separately. Stop/revoke the meeting route on a scope leak, ambiguous repeated
operation or failed recording coverage; preserve native work and evidence.

The current admission record is a replay hold, not a complete crash-recovery journal.
It does not itself bind every queued worker receipt to a restored voice thread.
Inspect retained worker/transcript receipts after a crash; automatic resumed
operation and transparent media recovery remain outside this prototype.
