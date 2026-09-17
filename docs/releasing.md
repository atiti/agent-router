# Releasing AgentRoute

AgentRoute releases patched open-source Codex binaries, the AgentRoute wheel, a matching official
Code Mode host, checksums, and build-provenance attestations. They never contain ChatGPT.app,
credentials, user configuration, transcripts, or audit data.

## Release gate

1. Let CI pass, including the pinned patch-stack check and wheel-content check.
2. Run `scripts/check_upstream.sh <candidate-codex-tag>` and review every patch conflict.
3. Test GPT subscription, Azure, and DeepSeek turns; a provider switch; a tool continuation; an
   automatic approval review; a subagent route; compaction; and a resumed mixed-provider session.
4. Run `agentroute desktop rebuild`, verify the route banner in Desktop, and verify mobile remote
   connection against the routed app-server.
5. Confirm `codex --version` matches the Desktop compatibility version in
   `codex-package-version.patch`.
6. Update the AgentRoute version and changelog, commit, and tag `vX.Y.Z`.

Pushing the tag builds Linux and macOS payloads for arm64 and x64, creates SHA-256 files, emits
GitHub provenance attestations, and publishes release assets without allowing an existing asset to
be overwritten. No scheduled workflow creates a release.

Consumers can verify an archive with `gh attestation verify ARCHIVE --repo atiti/agent-router` in
addition to the checksum enforced by the installer.

## Apple signing

Without signing secrets, macOS binaries receive the local ad-hoc signature used by the source
installer. For public signed binaries, configure these Actions secrets:

- `APPLE_CERTIFICATE_BASE64`: base64-encoded Developer ID Application `.p12`
- `APPLE_CERTIFICATE_PASSWORD`: `.p12` password
- `APPLE_KEYCHAIN_PASSWORD`: temporary CI keychain password
- `APPLE_SIGNING_IDENTITY`: exact Developer ID Application identity

The Desktop app itself is always created locally from the user's official installation. Users may
choose an ad-hoc signature or their own Apple identity with `agentroute desktop install
--signing-identity ...`.

## Rollback

GitHub releases are immutable and older assets remain downloadable. The local installer preserves
the original `codex` as `codex-stock`; Desktop rebuilds preserve timestamped app backups under
`~/.agentroute/backups/desktop/`. Use `agentroute desktop rollback` for the Desktop app, or download
and install a prior release asset for the CLI.
