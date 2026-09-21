# Contributing

AgentRoute is alpha infrastructure around a pinned open-source Codex build. Start with a focused
issue for changes that affect provider boundaries, authentication, installation, or the maintained
Codex patch stack. Small documentation and test improvements can go directly to a pull request.

By participating, you agree to follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). For support and
safe diagnostic-sharing guidance, see [SUPPORT.md](SUPPORT.md).

Create focused changes, add tests for behavioral changes, and run:

```sh
uv run pytest
uv run ruff check .
```

Routing changes must remain deterministic and explainable. Add a reason code for a new signal,
avoid sending prompt data to external services, and preserve fail-open hook behavior.

When updating the Codex patch, update its pinned commit in `src/agentroute/codex_patch.py` and
`scripts/install.sh`, run the relevant Rust checks, and export the same final diff to both patch
copies. Contributions are licensed under Apache-2.0.

Before a release, follow [docs/releasing.md](docs/releasing.md). The scheduled upstream workflow is
an early-warning system, not an automatic release authority: a human must verify CLI routing,
provider boundaries, approval review, Desktop app-server behavior, and mobile remote connectivity.
