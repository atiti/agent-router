# Antigravity (Gemini) backend

Routes Codex through the Antigravity CLI login, using the existing Google
subscription quota with OAuth. No Gemini API key is involved.

```sh
agentroute antigravity check
agentroute antigravity install
```

`check` validates the credential and prints the tier mapping. `install`
registers the backend and starts a launchd agent (or systemd user unit) on
`127.0.0.1:8816`.

| Tier | Model | Reasoning |
| --- | --- | --- |
| FAST | `gemini-3.8-flash-low` | low |
| NORMAL | `gemini-3.8-flash-medium` | medium |
| SMART | `gemini-3.8-flash-high` | high |
| MAX | `gemini-4-hydrogen-high` | xhigh |

Installation preserves automatic tier routes. To opt in, use the standard
commands:

```sh
agentroute backend-route normal antigravity
agentroute backend-default antigravity
```

Confirm the current model list with `agy models` before changing a mapping.

## Credential

The bridge reads the macOS Keychain entry `service=gemini, account=antigravity`.
That token carries `https://www.googleapis.com/auth/aicode`, which the
Antigravity service requires.

`~/.gemini/oauth_creds.json` belongs to the Gemini CLI OAuth client and has a
different scope set. It is only accepted when it actually carries `auth/aicode`.
If neither source is usable, the error names every source it tried and how to
refresh it. Sign in once with `agy` to renew the credential.

## Why inference is delegated

Antigravity's own endpoint rejects replay by third-party clients. A direct
`v1internal:streamGenerateContent` call with a valid token, the managed project,
and Antigravity's user agent returns `403 The caller does not have permission`.
The bridge therefore runs `agy` and translates its result into Responses
events.

## Tool calling

**This backend is text-only.** `agy` runs its own tool loop and returns finished
text, so Codex receives no `function_call` items and cannot execute its own
shell or patch tools on this backend. A turn that asks for a tool will produce
text describing the action rather than performing it.

The catalog therefore declares `tool_calling = "none"` for these models, which
keeps agentic work on backends that can execute tools. Use this backend for
review, analysis, and drafting. Real tool support needs either the Antigravity
wire protocol or ACP integration with an explicit approval boundary.

## Operational notes

The service resolves `agy` to an absolute path at install time, because launchd
starts it with a minimal `PATH`. Each request starts one `agy` turn, and a
first-run turn reports roughly 10,000 input tokens of CLI overhead.

```sh
curl -sS http://127.0.0.1:8816/healthz
agentroute antigravity uninstall
```
