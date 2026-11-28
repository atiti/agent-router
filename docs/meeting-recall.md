# Recall operator for the meeting prototype

`scripts/meeting_recall.py` creates one explicitly requested Google Meet participant, inspects it, removes it from the call, and collects its finalized recording into private local custody. It complements the [scoped meeting media prototype](meeting-prototype.md). The participant page supplies microphone audio to the existing Voice connection; the native worker retains business permissions and task state.

This is a generic operator example in the source checkout, implemented using the Python standard library on macOS/Linux. It does not provision a Recall account/key, change grants, run a worker, configure public ingress, install services or start a supervision loop. Source and local fixture verification do not establish live Meet acceptance.

## Inputs and account checks

Use the already-authorized Recall workspace and its actual region. Obtain the REST key through the governed secret route; a Recall MCP key cannot call the Bot REST API. Before a real create, verify the workspace's API version, account identity, current credit balance, bot allowance and recording/participant authorization. `--workspace-api-version 1.11` is a required operator attestation. Older or unknown versions are rejected; this script does not migrate a legacy account.

The REST key is injected through `RECALL_API_KEY` or a `0600` key file owned by the current OS user. Key contents never belong in a command argument. The regional API origin is fixed by `--region`; there is no arbitrary base URL flag. REST redirects and environment proxies are refused so a credential cannot follow a provider-supplied pagination link to another host.

Prepare existing directories, owned by the current user with mode `0700`, for operation state and recording artifacts. Keep these with the project's private records, outside shared logs and Git. The script creates an operation UUID subdirectory and `0600` files. Symlinked or group/world-readable input/state files are rejected. Run collection as the same established worker identity or provide access through the existing artifact owner; the script grants no new users access.

Use `--page-url https://meeting.example.com/ --meeting-key-file /private/operator/meeting-access.key`. The operator reads the existing scoped meeting key without printing it and adds the `#access=` fragment expected by the prototype. The page URL must have no query, fragment or embedded credentials. Do not place a credential-bearing URL in shell history, a ticket or a shared log.

The immutable original request is retained in `request.private.json` with mode `0600`. This file contains the private participant URL so reconciliation can compare the exact original request. `operation.json` and command output contain IDs, hashes and fixed status codes, never the private URL, REST key, raw provider response or signed media URLs. Treat the whole state directory as private nonetheless.

## Local preflight and controlled create

Use a stable, canonical UUID for this single logical bot operation. The following placeholder values must be replaced with the selected private directories, actual Meet URL and prepared participant page. `preflight` validates inputs without provider calls or creating operation files:

```sh
python3 scripts/meeting_recall.py \
  --state-dir /private/operator/recall-state \
  --operation 00000000-0000-4000-8000-000000000001 \
  --region us-west-2 --workspace-api-version 1.11 \
  preflight \
  --meeting-url https://meet.google.com/abc-defg-hij \
  --page-url https://meeting.example.com/ \
  --meeting-key-file /private/operator/meeting-access.key
```

For the already-authorized controlled call, use the same arguments with `create` in place of `preflight`; inject the REST key through the environment or add the global `--api-key-file` option before the subcommand. A successful create confirms the returned bot identity and binding. It does not establish admission, speech, recording coverage, or any business task.

The request selects `web_4_core`, camera output from the HTTPS webpage, mixed MP4 and MP3, and `include_bot_in_recording.audio=true`. It requests Recall streaming transcription in `prioritize_accuracy` mode with automatic language detection and separate-stream diarization when available. This transcript is for the recording; the conversation receives audio directly through the webpage, without waiting for transcription. The script deliberately omits `output_media.audio`, which would route meeting audio to a different hosted agent.

This controlled operator fixes the trial at 900 recorded seconds and 180 seconds for waiting/no participants/no recording. These are separate phase limits, not a single total lifetime guarantee. Retention is seven days. Larger bots and transcription have usage charges; account credit and trial coverage must be established before create. Do not add a payment method merely to obtain a key: Recall says this enables automatic top-ups and may charge immediately.

`create` dispatches an ad-hoc bot; it can fail for capacity. Production on-time guarantees require scheduling, which is outside this controlled operator slice. The host must admit the guest where the meeting's policy requires it. No signed-in Google identity or Workspace policy change is configured here.

## Effects and recovery

All commands use the same state directory, operation UUID, region and verified version. A nonblocking file lock serializes operations. A changed payload, region or saved request is rejected. An attempt to acquire an already-held lock exits with `operation_busy`.

Before create, the operator durably writes original request bytes, SHA-256, a request-binding hash and an immutable idempotency key, then records an uncertain outcome before making the request. A second successful invocation returns the existing receipt and never creates another bot. If HTTP fails, times out, returns an incomplete response or the process crashes after claiming the effect, subsequent create calls hold. The script never automatically retries an effect, even within Recall's one-hour idempotency cache.

```sh
python3 scripts/meeting_recall.py \
  --state-dir /private/operator/recall-state \
  --operation 00000000-0000-4000-8000-000000000001 \
  --region us-west-2 --workspace-api-version 1.11 \
  reconcile --effect create
```

Reconciliation follows all bounded result pages filtered by `metadata__agentroute_meeting_operation=<UUID>`. It keeps the exact regional origin, endpoint and filter on every page. Exactly one bot must match the operation and request-binding metadata. A detailed read must also match the original meeting, page, recording settings, variant and timeouts. Zero, conflicting, duplicate or incomplete results remain held. Pagination loops and more than 100 pages remain held. This never converts an unproven absence into permission to create again.

Recall may later clear the original meeting URL. An ambiguous create that cannot be matched after that cannot be adopted automatically. Preserve the journal and resolve the original operation through the account owner; do not delete its state or invent a new UUID to escape the hold. Recall matches idempotency by key/method/path, ignores changed request bodies and caches for only one hour, so it is not an indefinite operation ledger.

Use `status` with the same global arguments for a one-shot exact-bot read. Output exposes only known status values and recording IDs. It does not poll indefinitely or expose meeting content. Current Recall guidance requires webhooks for production lifecycle monitoring; this slice is an explicit operator, not a monitoring daemon.

Use `leave` with the same global arguments to call `/bot/{id}/leave_call/`. It first verifies the exact saved bot and commits its own idempotency key/uncertain state. This removes the bot from the call without deleting recordings. An uncertain response remains held; use `reconcile --effect leave`. Reconciliation only observes whether that same bot has ended and does not claim our request caused the exit. Media exit does not interrupt the native work session.

## Recording collection and existing-worker access

After the bot ends, invoke:

```sh
python3 scripts/meeting_recall.py \
  --state-dir /private/operator/recall-state \
  --operation 00000000-0000-4000-8000-000000000001 \
  --region us-west-2 --workspace-api-version 1.11 \
  collect --artifact-dir /private/operator/meeting-artifacts
```

The command gets every recording listed on the saved bot, including multiple recordings produced by stop/start. Each recording must identify that bot and the saved request binding. Both the recording and all three requested artifacts must be `done`; a missing, failed, deleted or still-processing artifact leaves collection incomplete.

Downloads are ordinary HTTPS GETs with **no Recall Authorization header**. Redirects, embedded URL credentials, literal private addresses and non-HTTPS URLs are refused. The provider-selected HTTPS media host is trusted as part of the authenticated Recall response; this example is not a general downloader for untrusted URLs. The maximum is 512 MiB per binary artifact; transcripts are capped at 32 MiB and must parse as a JSON array. A returned content length must match the downloaded byte count. Recordings are not transcoded or semantically quality-scored here.

Files stage in a new `0700` directory with `0600` media files. The operator rechecks the bot's recording set after download, verifies all content hashes and sizes, then atomically publishes the complete directory and manifest. The complete result is:

```text
<artifact-dir>/<operation-uuid>/
  manifest.json
  <recording-uuid>/
    video.mp4
    audio.mp3
    transcript.json
```

The manifest binds operation, bot, region, request hash, recording/media IDs, private relative paths, byte sizes and SHA-256 hashes. It contains no download URLs. Repeated collection and completed-status reads recheck the current bot/recording/media set; changed or failed provider artifacts hold the result as incomplete while preserving retained files. Pass its private local path to the existing authorized worker to read the recording and transcript. It is also a suitable input to the existing artifact owner's import operation; no second storage system is created.

Ordinary failed downloads remove their partial staging directory and never publish a complete manifest. An explicit rerun may refresh expired signed URLs through a new read and retry collection, without repeating create/leave. After a crash following the final atomic rename, rerunning collection adopts only a complete manifest matching the saved recording/media IDs and verified bytes. A modified or missing file downgrades the result to incomplete. A hard kill before final rename can leave a hidden private staging directory; it is never adopted or reported complete. Preserve it for inspection, then remove only that known partial attempt.

The request enables recording Aura's voice; audible coverage still requires the live test. Recording the bot's own camera tile is separately gated by Recall and is **not** promised by `include_bot_in_recording.audio`. The webpage microphone excludes the bot's audio; feeding an `audio_mixed_raw` stream that includes the bot back into Voice could cause feedback.

## Remaining deployment boundary and checks

An existing authenticated Recall binding, credit/version verification, approved reachable participant page, ready native worker and actual live Meet test are still required. No account/role migration or webhook endpoint is created by this script. If the existing owner invokes collection from webhooks, it must verify the correct account's signature before calling the operator: older Recall accounts use endpoint-specific Svix credentials for dashboard webhooks, while newer workspaces use workspace verification secrets. Do not expose this CLI as an unauthenticated public endpoint.

Local verification:

```sh
uv run --no-sync pytest tests/test_meeting_recall.py
uv run --no-sync ruff check scripts/meeting_recall.py tests/test_meeting_recall.py
```

The HTTP fixtures cover pre-send journals, uncertain create/leave, immutable request bindings, serialized execution, complete pagination and duplicate detection, cross-origin/redirect refusal, sanitized errors and status, recording identity/readiness, expired/truncated downloads, private files and complete-manifest recovery. They use loopback fixture servers, never a provider account.

Primary contracts checked on 27 November 2026: [authentication](https://docs.recall.ai/reference/authentication), [Create Bot](https://docs.recall.ai/reference/bot_create), [custom webpage](https://docs.recall.ai/docs/meeting-agents-build-a-custom-meeting-agent), [bot audio recording](https://docs.recall.ai/docs/meeting-agents-include-meeting-agents-in-recordings), [idempotency](https://docs.recall.ai/reference/idempotency), [artifact downloads](https://docs.recall.ai/docs/download-schemas), [Retrieve Recording](https://docs.recall.ai/reference/recording_retrieve), [Remove Bot From Call](https://docs.recall.ai/reference/bot_leave_call_create), [API versions](https://docs.recall.ai/docs/api-v111-release-notes), [webhook authentication](https://docs.recall.ai/docs/authenticating-requests-from-recallai), and [billing](https://docs.recall.ai/docs/billing-faq).
