# Releasing AgentRoute

AgentRoute releases patched open-source Codex binaries, the AgentRoute wheel, a matching official
Code Mode host, checksums, and build-provenance attestations. They never contain ChatGPT.app,
credentials, user configuration, transcripts, or audit data.

For the upstream mirror, downstream development branch, and stable-release port workflow,
see [Maintaining the Codex fork](codex-fork.md).

## Release gate

1. Let CI pass, including the pinned commit-stack check and wheel-content check.
2. Run `scripts/check_upstream.sh <candidate-codex-tag>` and review every rebase conflict
   and the resulting `range-diff`. Update the immutable fork/upstream pins and matching
   Code Mode host version together; keep upstream's real CLI version unchanged.
3. Test GPT subscription, Azure, and DeepSeek turns; a provider switch; a tool continuation; an
   automatic approval review; a subagent route; compaction; and a resumed mixed-provider session.
4. Run `agentroute desktop rebuild`, verify the route banner in Desktop, and verify mobile remote
   connection against the routed app-server.
5. Confirm the routed binary and bundled Desktop CLI report the same Codex
   `major.minor.patch` release line; prerelease/build suffixes may differ. Then verify the mobile
   remote connection against the rebuilt Desktop app, since matching release lines alone do not
   guarantee app-server protocol compatibility.
6. Update the AgentRoute version and changelog, commit, and tag `vX.Y.Z`.

Pushing the tag builds Linux and macOS payloads for arm64 and x64, creates SHA-256 files, emits
GitHub provenance attestations, and publishes release assets without allowing an existing asset to
be overwritten. No scheduled workflow creates a release.

Consumers can verify an archive with `gh attestation verify ARCHIVE --repo atiti/agent-router` in
addition to the checksum enforced by the installer.

## Apple signing

Public macOS release builds fail closed without signing secrets. Configure these Actions secrets:

- `MACOS_CERTIFICATE_P12_BASE64`: base64-encoded Developer ID Application `.p12`
- `MACOS_CERTIFICATE_PASSWORD`: `.p12` password
- `MACOS_CODESIGN_IDENTITY`: exact Developer ID Application identity
- `APPLE_ID`: Apple developer account email
- `APPLE_TEAM_ID`: Apple Developer team identifier
- `APPLE_APP_SPECIFIC_PASSWORD`: app-specific password used by `notarytool`

These names intentionally match Overwatchr's signing setup. Do not tag a public release until all
six are present. Each macOS matrix job imports the certificate into an ephemeral keychain, signs
both native binaries with hardened runtime and a secure timestamp, submits their CDHashes for Apple
notarization, and requires Gatekeeper assessment to pass. Ad-hoc signatures remain appropriate for
local source builds, but are deliberately rejected by the release artifact builder because a
downloaded ad-hoc binary can be blocked by macOS Gatekeeper.

The Desktop app itself is always created locally from the user's official installation. Users may
choose an ad-hoc signature or their own Apple identity with `agentroute desktop install
--signing-identity ...`.

## Rollback

GitHub releases are immutable and older assets remain downloadable. The local installer preserves
the original `codex` as `codex-stock`; Desktop rebuilds preserve timestamped app backups under
`~/.agentroute/backups/desktop/`. Use `agentroute desktop rollback` for the Desktop app, or download
and install a prior release asset for the CLI.
