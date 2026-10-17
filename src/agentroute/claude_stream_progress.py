"""Observe Claude stream liveness without logging prompts or generated content."""

import time
from collections.abc import Callable, Iterator
from typing import Any

PROGRESS_TIMEOUT_SECONDS = 120
PROGRESS_LOG_SECONDS = 15


class ClaudeStreamError(RuntimeError):
    """An upstream stream stopped making progress or ended without a terminal event."""


def observe_stream(
    events: Iterator[tuple[str, dict[str, Any]]],
    *,
    logger: Callable[[str], None],
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Count progress and reject endless keepalives; network reads also need a timeout."""
    started = last_progress = last_log = clock()
    count = characters = 0
    phase = "waiting_for_message"
    terminal = False
    try:
        for event, data in events:
            now = clock()
            count += 1
            progress = False
            previous_phase = phase
            if event == "message_start":
                phase = "message_started"
                progress = True
            elif event == "content_block_start":
                phase = str((data.get("content_block") or {}).get("type") or "content")
                progress = True
            elif event == "content_block_delta":
                delta = data.get("delta") or {}
                size = sum(
                    len(str(delta.get(key) or "")) for key in ("text", "thinking", "partial_json")
                )
                characters += size
                progress = size > 0
            elif event in {"content_block_stop", "message_delta", "message_stop", "error"}:
                progress = True
            if progress:
                last_progress = now
            if phase != previous_phase or now - last_log >= PROGRESS_LOG_SECONDS:
                logger(
                    f"progress phase={phase} events={count} chars={characters} "
                    f"elapsed={now - started:.0f}s idle={now - last_progress:.0f}s"
                )
                last_log = now
            if now - last_progress >= PROGRESS_TIMEOUT_SECONDS:
                raise ClaudeStreamError(
                    f"Claude made no model progress for {PROGRESS_TIMEOUT_SECONDS}s "
                    f"(phase={phase}); keepalives do not count as progress. Retry this turn."
                )
            terminal = terminal or event in {"message_stop", "error"}
            yield event, data
        if not terminal:
            raise ClaudeStreamError(
                "Claude stream ended without a completion event. Retry this turn."
            )
    finally:
        logger(
            f"stream ended phase={phase} events={count} chars={characters} "
            f"elapsed={clock() - started:.0f}s terminal={terminal}"
        )
