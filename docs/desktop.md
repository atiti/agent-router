# Codex Desktop with AgentRoute

AgentRoute 0.5.58 bundles the patched Codex 0.159.2 runtime and matching
Code Mode host. It supports the official app's legacy and nested CLI package layouts.

## Upgrade an existing installation

1. Save work and fully quit both the official and routed Desktop apps.
2. Run these commands separately:

```sh
agentroute update
agentroute desktop rebuild
agentroute desktop status
agentroute doctor
open -a /Applications/ChatGPT-Routed.app
```

`agentroute update` checksums and installs the published CLI release. The rebuild copies
your official `/Applications/ChatGPT.app`, replaces the actual CLI entrypoints, signs the
copy, and verifies its runtime and app-server before publishing it. A rollback copy is
kept under `~/.agentroute/backups/desktop/`. Updating the CLI alone does not update Desktop.

For a first installation, use `agentroute desktop install` instead of `rebuild`.
If the official app is elsewhere, pass `--source /path/to/App.app` to both status and rebuild.

## Confirm routing works

Status should show source and destination on the `0.159.2` release line, the routed runtime
at `0.159.2`, `Destination Matches Runtime: True`, and runtime v49 in both build IDs.
The doctor Desktop check should pass. Open the routed app explicitly; both apps share a
bundle identifier, so an already running official app can intercept an open request.

Start a new turn with `@smart Reply ready`. Hook stats should show a successful
UserPromptSubmit and a model route notice should appear. `agentroute history` reports
whether the route was actually applied. A successful Stop hook alone does not prove routing.

## Troubleshooting

- **Invalid UserPromptSubmit JSON:** the active Desktop runtime may be stock or stale.
  Fully quit, rebuild, and launch the routed copy. Check the embedded build, not only the CLI version.
- **Source version unknown:** status reports the resolved executable path. Run that path with
  `--version` for its error. A missing executable is reported separately from a version mismatch.
- **Release line mismatch:** update the official app to a matching version, or use a matching
  patched runtime. `--allow-version-mismatch` is an experimental bypass and does not establish
  frontend/app-server compatibility.
- **Restore the prior routed app:** fully quit Desktop, then `agentroute desktop rollback`.

## Claude and usage profiling

For Claude subscription setup, see the complete [Claude bridge guide](claude-bridge.md).
Use `agentroute bridge install`, then `agentroute bridge check`, and start a turn with `@claude`.

The optional profiler is disabled by default. Enable with `agentroute context profile on`,
then restart the routed app to activate native request capture. Inspect
`agentroute context profile report --days 1`. Raw request traces stay private on the local
machine; reports summarize counts. USD figures estimate API equivalents, not subscription
charges or quota debits. See [usage profiling](usage-profiling.md).
