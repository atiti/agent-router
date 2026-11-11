# Final Codex 0.167 runtime source

The source installer pins `b5c5c2741fce4f94665afaad1b7f54dbdc4e5265`, the normal merge of atiti/codex PR42 into `agentroute-release-0.167.0`. Its Git tree is `a7759982cbce89954ad3d112eba66a9cf69c335e`. Codex `main` remains an upstream mirror. The runtime build ID is `b5c5c2741fce4f94665afaad1b7f54dbdc4e5265-provider-routing-v68`.

PR42 consolidates the provider-routing, routed compaction, visible review responses and supervisor continuation changes. The individual PR32-41 merge sequence is superseded. Its final Fork CI built CLI, app-server and Code Mode companion and ran focused tests on macOS ARM and Linux x86. This does not claim full upstream test-suite coverage.

The installer builds the CLI, which includes its app-server subcommand, from the immutable source. The default Code Mode companion remains the official platform package pinned to version `0.167.0`; `AGENTROUTE_CODE_MODE_HOST` accepts a separately verified executable, including the companion built from the same fork source. The actual framed handshake and JavaScript execution smoke must pass before installed acceptance. A matching package version alone is insufficient evidence.

A cached output path is not source provenance. Rebuild the final commit before staging artifacts; record source commit/tree, clean status, build profile, compiler and artifact hashes. Preserve existing build caches and active sessions.

Source delivery and runtime activation are separate gates. Coordinate a safe window with the integration supervisor before replacing the shared runtime. Final acceptance includes installed binary/build/helper identity, real Code Mode execution, role routing and supervisor overlap, restart, duplicate, blocked-delivery, unknown-outcome, late-enrollment and deadline scenarios. Retain external-effect holds throughout acceptance.
