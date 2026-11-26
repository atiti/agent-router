# Gemini backend (direct Google API)

Routes Codex through the Gemini API with Google OAuth. The bridge calls
`generativelanguage.googleapis.com` (or Vertex AI) itself, translates Responses
requests into `generateContent` calls, and returns Gemini's native
`functionCall` parts as Codex tool calls. Nothing shells out to `agy`,
Antigravity, or any other CLI.

```sh
agentroute gemini login
agentroute gemini check
agentroute gemini install
```

`check` refreshes the token, lists the models the credential may use, and sends
one live prompt. `install` discovers real model IDs, registers the backend and
starts a launchd agent (or systemd user unit) on `127.0.0.1:8817`.

## One-time OAuth client

The Gemini API authenticates OAuth tokens and bills usage to the project that
owns the OAuth client, so a Desktop client is created once:

1. Enable the Generative Language API in
   <https://console.cloud.google.com/apis/library/generativelanguage.googleapis.com>.
2. Create an OAuth client of type **Desktop app** in
   <https://console.cloud.google.com/apis/credentials>.
3. Download the client JSON to `~/.agentroute/gemini-oauth-client.json`
   (or pass `--client-id` and `--client-secret`), run `agentroute gemini login`,
   and approve the printed URL.

The login uses PKCE, a `state` parameter, and a loopback callback on
`127.0.0.1:8818`. The refresh token is stored in
`~/.agentroute/gemini-oauth.json` with mode `0600`. Rotated refresh tokens are
persisted automatically. No API key is used.

## Transports

| `AGENTROUTE_GEMINI_TRANSPORT` | Endpoint | Credential |
| --- | --- | --- |
| `gemini` (default) | `generativelanguage.googleapis.com` | OAuth client with `generative-language` scope |
| `vertex` | `aiplatform.googleapis.com` | Google token with `cloud-platform` scope |

Vertex needs a project where the account may call
`aiplatform.endpoints.predict`:

```sh
export AGENTROUTE_GEMINI_TRANSPORT=vertex
export AGENTROUTE_GEMINI_VERTEX_PROJECT=my-project
export AGENTROUTE_GEMINI_VERTEX_LOCATION=global
export AGENTROUTE_GEMINI_TOKEN_FILE=~/.gemini/oauth_creds.json
agentroute gemini check
agentroute gemini install
```

`install` copies the selected transport variables into the service definition,
because launchd and systemd do not inherit your shell environment.

## Models

Tier mapping is discovered from the API rather than hardcoded, because Google
renames and retires models. The newest chat model in each family wins:

| Tier | Family preference | Reasoning |
| --- | --- | --- |
| FAST | flash-lite, then flash | low |
| NORMAL | flash | medium |
| SMART | pro, then flash | high |
| MAX | pro, then flash | xhigh |

`agentroute gemini models --refresh` prints the available models and the chosen
mapping. Override it with `AGENTROUTE_GEMINI_MODELS='{"normal":"gemini-2.5-flash"}'`
or by editing the backend tiers in `~/.agentroute/config.yaml`. The discovery
cache lives in `~/.agentroute/gemini-models.json`.

Gemini 3 reasoning parts marked `thought` are dropped; they never leak into
assistant output. Thinking controls are sent only for the families that
document them (`thinkingLevel` for Gemini 3, `thinkingBudget` for 2.5).

## Tool calling

Tool declarations keep Gemini's uppercase OpenAPI types. Freeform tools such as
`apply_patch` are declared with one string `input` argument and returned as
Responses `custom_tool_call` items, so Codex applies patches exactly as it does
with other providers. Several `functionCall` parts in one Gemini response become
several `function_call` items in order.

## Using the backend

Installation preserves automatic tier routes. To opt in:

```sh
agentroute backend-route normal gemini
agentroute backend-default gemini
```

Or address it per run:

```sh
codex -c model_provider=agentroute-gemini -m gemini-2.5-pro
```

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ACCESS_TOKEN_SCOPE_INSUFFICIENT` | Token lacks `generative-language` | Rerun `agentroute gemini login` with your own Desktop client |
| `invalid_grant` | Refresh token revoked or client changed | Rerun `agentroute gemini login` |
| `the stored credential lacks ...` | Login used a client without the scope | Rerun login and approve the Gemini scope |
| `RESOURCE_EXHAUSTED` | Project quota | Wait, or use a project with quota |
| `404 model not found` | Cached model retired | `agentroute gemini models --refresh` then reinstall |

`curl -s http://127.0.0.1:8817/healthz` reports the transport, credential
status, resolved tier mapping, and whether native tool calling is active.
