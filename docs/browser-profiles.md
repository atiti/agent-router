# Browser profile bindings

An agent that uses a browser needs to know which of your existing browser profiles it may use.
`agentroute browser` gives each profile a short alias, stores which kind of automated use that
alias permits, and resolves the alias to the bundled ChatGPT browser plugin's launch and check
settings. AgentRoute does not drive the browser, sign in, copy cookies or decide which
account belongs to a profile.

## Commands

```sh
agentroute browser profiles                 # profile directories and their display labels
agentroute browser profiles --json --include-email
agentroute browser bind ALIAS --profile-dir "Profile 3" [--site HOST]... [--workflow NAME]...
agentroute browser mode ALIAS off|browser|computer|both
agentroute browser bindings [--json]
agentroute browser resolve ALIAS [--json]
agentroute browser resolve --site HOST [--workflow NAME] [--json]
agentroute browser resolve ALIAS --env      # shell exports for plugin launch/check helpers
agentroute browser open ALIAS [--dry-run] [--json]
agentroute browser unbind ALIAS
```

`profiles` reads the browser's `Local State` and lists only profile directories that still exist.
The signed-in email appears only with `--json --include-email`; nothing copies it into a binding.

`bind` accepts only `Default` or `Profile N` directories that exist under the user-data root.
Use `--user-data-dir` for a non-default Chromium root; otherwise AgentRoute uses
`CODEX_CHROMIUM_USER_DATA_DIR` or the platform's Google Chrome root. `--site` and `--workflow`
labels let `resolve` find the one binding meant for a site or workflow. Rebinding an alias replaces
its profile and labels and keeps its mode unless `--mode` is given.

`mode` records what the alias permits: `off`, `browser` (the default for new bindings),
`computer`, or `both`. Integrations must check it before acting; `resolve --env` refuses aliases
whose mode does not include browser use.

`resolve` checks again that the profile exists and prints the alias, mode, profile directory,
user-data root, the matching `--profile-directory` launch argument and these plugin overrides:

- `CODEX_CHROMIUM_USER_DATA_DIR`: the browser's user-data root.
- `CODEX_CHROMIUM_PREFERENCES_PATH`: the bound profile's `Preferences` file.

It also runs the browser plugin's own `check-extension-installed.js` against that one profile and
reports `enabled`, `disabled`, `not_installed` or `unavailable`. Authentication is always reported
as `not checked`. Set `AGENTROUTE_BROWSER_PLUGIN_DIR` when the plugin is not in the default Codex
plugin cache. Use `--no-check-extension` to skip the check.

Selection fails instead of guessing when an alias is unknown, its profile directory is gone, or a
site/workflow label matches no binding or more than one.

`open` requests an `about:blank` Chrome window in the bound profile through the vendor's existing
`open-chrome-window.js`. It requires `browser` or `both` mode and an enabled extension. It first
performs a vendor dry run and checks the reported profile and launch arguments against the binding.
`--dry-run` stops there and reports the verified command without opening a window. Bindings outside
Chrome's standard user-data root are refused when the vendor opener cannot forward that root.

The profile environment variables select launch/check targets. They do not restrict live browser
connections, and the saved mode does not change permissions or capabilities of an existing session.
Opening a window does not authenticate or connect a terminal session. A successful real invocation
reports `open_requested`; it does not claim the window was observed or the terminal connected.

## Storage

Bindings live in `$AGENTROUTE_HOME/browser-profiles.json` (default `~/.agentroute`). The file is
written atomically with mode 0600, and its directory is kept at 0700. AgentRoute refuses a sidecar
that is a symlink, readable by other users, a different schema version or contains unknown fields,
and leaves such a file unchanged.

Python integrations can call `agentroute.browser_profiles.resolve_binding(alias)` or
`resolve_binding(site=..., workflow=...)`; check `browser_allowed` or `computer_allowed` on the
result before using its `env`.
