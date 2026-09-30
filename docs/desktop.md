# Codex Desktop with AgentRoute

AgentRoute 0.5.59 bundles the patched Codex 0.159.2 runtime and matching
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
copy, and verifies that its embedded CLI launches the routed runtime and that the app-server
starts. Different official-app and routed-runtime versions produce a warning and the rebuild
continues by default. The copy is kept under `~/.agentroute/backups/desktop/` for rollback.
Updating the CLI alone does not update Desktop.

The version warning is informational because exact patch-line equality is a conservative proxy,
not a verified protocol-compatibility signal. A successful build checks startup, not every Desktop
workflow. After rebuilding, launch the routed app and test a normal routed turn; test mobile remote
too if you use it. To make a release-line mismatch block the build, pass
`--strict-version-match` to `desktop install` or `desktop rebuild`.

For a first installation, use `agentroute desktop install` instead of `rebuild`.
If the official app is elsewhere, pass `--source /path/to/App.app` to both status and rebuild.

## Confirm routing works

Status shows the official source version separately from the routed runtime. Their release-line
match can be `False` when the installed official app is older; this is allowed by default.
The rebuilt destination should report the same version as `Routed Binary Version`, show
`Destination Matches Runtime: True`, and carry the current runtime build ID. The doctor Desktop
check should pass. Open the routed app explicitly; both apps share a
bundle identifier, so an already running official app can intercept an open request.

Start a new turn with `@smart Reply ready`. Hook stats should show a successful
UserPromptSubmit and a model route notice should appear. `agentroute history` reports
whether the route was actually applied. A successful Stop hook alone does not prove routing.

## Troubleshooting

- **Invalid UserPromptSubmit JSON:** the active Desktop runtime may be stock or stale.
  Fully quit, rebuild, and launch the routed copy. Check the embedded build, not only the CLI version.
- **Source version unknown:** status reports the resolved executable path. Run that path with
  `--version` for its error. A missing executable is reported separately from a version mismatch.
- **Version mismatch warning:** by default the rebuild continues and the routed runtime remains
  embedded. Test the app after opening it because startup checks cannot certify every frontend or
  mobile-remote workflow. Use `--strict-version-match` when you want a mismatch to stop the build.
- **Restore the prior routed app:** fully quit Desktop, then `agentroute desktop rollback`.

## Claude and usage profiling

For Claude subscription setup, see the complete [Claude bridge guide](claude-bridge.md).
Use `agentroute bridge install`, then `agentroute bridge check`, and start a turn with `@claude`.

The optional profiler is disabled by default. Enable with `agentroute context profile on`,
then restart the routed app to activate native request capture. Inspect
`agentroute context profile report --days 1`. Raw request traces stay private on the local
machine; reports summarize counts. USD figures estimate API equivalents, not subscription
charges or quota debits. See [usage profiling](usage-profiling.md).
