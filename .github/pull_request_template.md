## Summary

- What changes?
- Why is this the smallest safe solution?

## Validation

- [ ] `uv run pytest`
- [ ] `uv run ruff check .`
- [ ] Relevant shell scripts pass `sh -n`
- [ ] Routing/provider boundary behavior was tested when applicable
- [ ] Documentation and changelog were updated when user-visible behavior changed

## Safety and privacy

- [ ] No credentials, raw prompts, transcripts, customer data, or generated local state are included
- [ ] Failure behavior remains explicit and fail-open/fail-closed boundaries are preserved
- [ ] Codex patch changes were validated against the pinned upstream commit
