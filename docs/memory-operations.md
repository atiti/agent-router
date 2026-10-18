# Runtime memory

`agentroute memory status` reads process RSS and, on macOS, physical footprint and lifetime peak.
Physical footprint includes compressed memory and GPU allocations that RSS can miss. Process
charges cannot be summed as a partition of physical RAM.

`agentroute memory watch --duration 3600 --interval 10 --threshold-mib 4096` records a bounded
one-hour time series and one `vmmap -summary` per Codex process identity that crosses 4 GiB.
Output is private and local under `~/.agentroute/profiling/memory`. It excludes process arguments,
environments, prompt text, and heap contents. This captures a future spike without restarting
active sessions. A memory-map summary identifies allocation categories; attribution to call sites
still requires a separate allocation profile in a controlled reproduction.

`agentroute memory playwright-install --original /absolute/path/to/playwright-cli` adds a launcher
under `~/.agentroute/bin`. Keep this directory before the original CLI in PATH. The launcher
preloads an in-memory compatibility patch: each tab retains at most 1,024 network request records
and 2,048 recent event records. Old entries are evicted in batches; network listing indices can
therefore change after eviction. Existing daemons retain their old code until closed and reopened.
The upstream package is preserved, and an incompatible collector implementation fails visibly.
Use the original launcher directly to roll back. The guard does not automatically stop idle
sessions, which could interrupt other work; close each disposable named session after use.

