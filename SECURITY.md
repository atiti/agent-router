# Security policy

Please report vulnerabilities privately through GitHub's security-advisory form for this
repository. Do not include API keys, transcripts, private prompts, or customer data in a public
issue.

AgentRoute release artifacts contain no credentials. The bootstrap and update paths verify a
published SHA-256 checksum before executing the bundled installer, and GitHub Actions emits build
provenance attestations. Provider credentials remain in environment variables or the owner-only
local AgentRoute credential file.

The routed Desktop app is derived locally from the user's official ChatGPT.app. AgentRoute does not
redistribute the proprietary application, reuse OpenAI signing credentials, or claim affiliation
with OpenAI.
