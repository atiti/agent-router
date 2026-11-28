# Doctor and setup fingerprints

`agentroute doctor` checks configuration, hooks, providers, and the routed runtime. It also
checks where the installation came from and whether it changed after installation.

## Why provenance checks exist

Two users with the same usage pattern, such as long multi-provider threads and terminal
computer use, can get very different results. In one investigation, configuration was not
the cause. The two environments had drifted in ways the earlier checks could not detect:

- the package had been installed from a scratch or pull-request checkout
- installed modules had been edited by hand
- a terminal multiplexer started a private or older runtime
- running processes still used binaries that had since been replaced
- the shared server had been started with a settings fingerprint that no longer matched the
  installed runtime, so new terminals could not attach until it restarted
- computer-use tooling pointed at a re-signed copy instead of the vendor-signed app

The checks below make these differences visible. Each check is local, read-only, and limited
in time. A check can warn but never fails, so these checks never change doctor's exit status.

## Checks

| Check | What it verifies |
| --- | --- |
| `install-provenance` | How the `agentroute` package was installed: from a release wheel, a package index, an editable checkout, a local directory, or a VCS URL. This comes from the package's `direct_url.json`. Editable, directory, and VCS installs warn. It also warns when code is imported from somewhere other than the installed package, for example through `PYTHONPATH`, or when the installed version differs from the version recorded at install time. |
| `install-integrity` | Checks every file listed in the installed package's `RECORD` against its sha256. Modified and missing files are listed by package-relative name. This finds modules that were edited by hand. |
| `runtime-integrity` | Checks the installed runtime files (`bin/codex-bin`, `bin/codex-code-mode-host`, and the `bin/codex` launcher) against `~/.agentroute/install-manifest.json`. A mismatch warns "binary changed since install". If the manifest is missing, which is normal for installs made before this feature, it warns that provenance is unknown. The existing `runtime` row still checks the build revision. |
| `running-runtimes` | Counts running processes of the installed runtime. It also lists other `codex`-like binaries (by executable path) and AgentRoute Python services such as the bridge and daemons. It warns when processes started before the current install, when a private or development runtime is running (a copy of `codex-bin` outside an app bundle, or a cargo `target/` build), or when `codex` on `PATH` is not the AgentRoute launcher. |
| `shared-server` | If a shared server is running, compares the fingerprint it recorded with the fingerprint the current installation would produce. Nothing is started and no diagnostics are pruned. On a mismatch, it lists safe steps: close attached clients, run `agentroute server stop`, then start the server again and relaunch clients. |
| `computer-use` | Reads MCP servers in the Codex config that relate to computer use, a node REPL, or browser automation. Detection matches server names, commands, and environment variables. It checks referenced commands, `*_PATH` variables, and trusted-service maps. Each path is checked for existence and, where required, for being executable. On macOS, `codesign` also reports the signature kind. A warning is raised for an ad-hoc or unsigned signature, a Team ID that differs from the enclosing app bundle's Team ID (read at runtime, not hardcoded), or a path inside the Desktop app that AgentRoute rebuilt. Signature checks are skipped on other platforms and when `codesign` is unavailable. |
| `accounts` | Warns when two enabled account profiles share the same masked account hash, which means a duplicate login that cannot add capacity. It also warns when a profile has no stored login or its login has not been refreshed in over 30 days. It reads only local metadata. |
| `compaction` | Confirms that the runtime revision is at or above `MIN_PORTABLE_COMPACTION_REVISION` (`provider-routing-v72`). That revision is the Codex 0.172 port in AgentRoute 0.5.87, which made compaction portable for routed histories that need provider-state isolation. |

## What the installers record

Release installs (`agentroute update` or a release archive's `install.sh`) and source builds
(`scripts/install.sh`) write `~/.agentroute/install-manifest.json` after the install has
committed. The manifest contains:

- the agentroute version and runtime build id
- the source: a release tag, or the source commit plus a dirty-tree flag
- the sha256 of each installed runtime file
- the install time

It contains no paths, credentials, or account data. If the manifest cannot be written, the
installer prints a note and the install itself is not affected.

## Comparing two machines

Ask the other person to run:

```sh
agentroute doctor --fingerprint > fingerprint.json
```

They send you `fingerprint.json`, and you run:

```sh
agentroute doctor --compare fingerprint.json        # grouped, human-readable differences
agentroute doctor --compare fingerprint.json --json # machine-readable differences
```

The fingerprint is a redacted JSON snapshot. It contains:

- platform and architecture
- agentroute version and install provenance
- runtime build id, runtime file hashes, and manifest status
- the names of modified package files
- a summary of running runtimes
- shared-server status and whether its fingerprint matches (not the fingerprint value)
- the configuration shape: backend names, enabled flags, provider ids, tier-to-model mappings,
  and classifier engine
- the number of account profiles, plus duplicate, missing, and stale counts
- the computer-use wiring: signature kind, Team ID match, and redacted paths
- the status of every doctor check

It never contains API keys or their environment values, base URLs, account identifiers,
emails, profile names, prompts, or hostnames. The home directory appears as `~` and other
user directories as `/<home>`. Review the file before sharing it anyway.
