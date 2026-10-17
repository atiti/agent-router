import pytest

from agentroute.claude_stream_progress import ClaudeStreamError, observe_stream


def test_keepalives_do_not_extend_progress_deadline():
    now = [0.0]
    logs = []

    def events():
        yield "message_start", {}
        yield "content_block_start", {"content_block": {"type": "thinking"}}
        for second in range(15, 136, 15):
            now[0] = second
            yield "ping", {}

    with pytest.raises(ClaudeStreamError, match="no model progress for 120s"):
        list(observe_stream(events(), logger=logs.append, clock=lambda: now[0]))
    assert "phase=thinking" in logs[-1]
    assert "chars=0" in logs[-1]
    assert "terminal=False" in logs[-1]


def test_long_reasoning_remains_live_without_logging_content():
    now = [0.0]
    logs = []

    def events():
        yield "message_start", {}
        yield "content_block_start", {"content_block": {"type": "thinking"}}
        for second in range(30, 601, 30):
            now[0] = second
            yield "content_block_delta", {"delta": {"thinking": "private reasoning"}}
        yield "message_stop", {}

    observed = list(observe_stream(events(), logger=logs.append, clock=lambda: now[0]))
    assert observed[-1] == ("message_stop", {})
    assert "terminal=True" in logs[-1]
    assert not any("private reasoning" in log for log in logs)


def test_truncated_stream_fails_instead_of_appearing_complete():
    with pytest.raises(ClaudeStreamError, match="without a completion event"):
        list(observe_stream(iter([("message_start", {})]), logger=lambda _: None))
