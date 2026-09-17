# Contributing

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
