import base64
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentroute.claude_bridge import REASONING_ITEM_PREFIX, ResponsesStream, translate_request
from agentroute.claude_thinking_store import ThinkingReplayError, load_thinking, store_thinking


@pytest.fixture
def store_path(monkeypatch, tmp_path):
    path = tmp_path / "private" / "replay.sqlite3"
    monkeypatch.setattr("agentroute.claude_thinking_store.thinking_store_path", lambda: path)
    return path


def _stream_thinking(request, block):
    stream = ResponsesStream(
        "resp_big_thinking",
        "claude-sonnet-5-5",
        request_body=request,
        profile_scope="second",
        profile_name="second",
    )
    stream.feed("content_block_start", {"index": 0, "content_block": block})
    events = stream.feed("content_block_stop", {"index": 0})
    item = json.loads(events[-1].split(b"data: ", 1)[1])["item"]
    return stream, item


@pytest.mark.parametrize(
    "block",
    [
        {"type": "thinking", "thinking": "analysis 🧠 " * 10_000, "signature": "sig" * 10_000},
        {"type": "redacted_thinking", "data": "opaque" * 10_000},
    ],
)
def test_large_signed_thinking_continues_tools_and_replays_verbatim(store_path, block):
    request = {"input": [{"type": "message", "role": "user", "content": "inspect the platform"}]}
    stream, item = _stream_thinking(request, block)
    assert not stream.terminal_error
    assert len(json.dumps(item)) < 1_000
    stream.feed(
        "content_block_start",
        {
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "call_read",
                "name": "read",
                "input": {},
            },
        },
    )
    stream.feed(
        "content_block_delta",
        {
            "index": 1,
            "delta": {
                "type": "input_json_delta",
                "partial_json": '{"path":"README.md"}',
            },
        },
    )
    stream.feed("content_block_stop", {"index": 1})
    completion = json.loads(stream.feed("message_stop", {})[-1].split(b"data: ", 1)[1])
    assert completion["type"] == "response.completed"
    continuation = {
        **request,
        "input": [
            *request["input"],
            *stream.items,
            {
                "type": "function_call_output",
                "call_id": "call_read",
                "output": "platform overview",
            },
        ],
    }
    # A fresh translator reads durable data; no stream or process cache is needed.
    payload, _, _ = translate_request(continuation, profile_scope="second")
    assert payload["messages"][1] == {
        "role": "assistant",
        "content": [
            block,
            {
                "type": "tool_use",
                "id": "call_read",
                "name": "read",
                "input": {"path": "README.md"},
            },
        ],
    }
    assert payload["messages"][2]["content"][0]["type"] == "tool_result"
    if store_path.stat().st_mode & 0o777 != 0o600:
        pytest.fail("thinking store must be private")
    assert store_path.parent.stat().st_mode & 0o777 == 0o700


def test_large_thinking_preserves_multiple_history_prefixes(store_path):
    request = {"input": [{"type": "message", "role": "user", "content": "go"}]}
    blocks = [
        {"type": "thinking", "thinking": "x" * 8_000, "signature": f"sig-{i}"} for i in range(2)
    ]
    items = []
    for block in blocks:
        _, item = _stream_thinking({**request, "input": [*request["input"], *items]}, block)
        items.append(item)
    payload, _, _ = translate_request(
        {**request, "input": [*request["input"], *items]},
        profile_scope="second",
    )
    assert payload["messages"][1]["content"] == blocks


def test_reference_cannot_cross_profile_or_history(store_path):
    request = {"input": [{"type": "message", "role": "user", "content": "go"}]}
    block = {"type": "thinking", "thinking": "x" * 8_000, "signature": "signed"}
    _, item = _stream_thinking(request, block)
    for scope, history in [
        ("default", request["input"]),
        ("second", [{"type": "message", "role": "user", "content": "different"}]),
    ]:
        payload, _, _ = translate_request({"input": [*history, item]}, profile_scope=scope)
        assert all(b["type"] != "thinking" for m in payload["messages"] for b in m["content"])
    envelope = json.loads(
        base64.urlsafe_b64decode(item["encrypted_content"].removeprefix(REASONING_ITEM_PREFIX))
    )
    envelope["profile"] = hashlib.sha256(b"default").hexdigest()[:16]
    forged = {
        **item,
        "encrypted_content": REASONING_ITEM_PREFIX
        + base64.urlsafe_b64encode(json.dumps(envelope).encode()).decode(),
    }
    with pytest.raises(ThinkingReplayError, match="scope"):
        translate_request({"input": [*request["input"], forged]}, profile_scope="default")


def test_missing_reference_is_an_explicit_failure(store_path):
    request = {"input": [{"type": "message", "role": "user", "content": "go"}]}
    _, item = _stream_thinking(request, {"type": "thinking", "thinking": "x" * 8_000})
    with sqlite3.connect(store_path) as db:
        db.execute("DELETE FROM blocks")
    with pytest.raises(ThinkingReplayError, match="missing"):
        translate_request({"input": [*request["input"], item]}, profile_scope="second")


def test_corrupted_reference_fails_integrity_check(store_path):
    digest = store_thinking(b'{"block":{"type":"thinking"}}')
    with sqlite3.connect(store_path) as db:
        db.execute("UPDATE blocks SET data = ?", (b"corrupted",))
    with pytest.raises(ThinkingReplayError, match="integrity"):
        load_thinking(digest)


def test_concurrent_deduplication_and_capacity_preserve_existing_blocks(store_path, monkeypatch):
    raw = b'{"block":{"type":"thinking"}}'
    monkeypatch.setattr("agentroute.claude_thinking_store.MAX_STORE_BYTES", len(raw))
    with ThreadPoolExecutor(max_workers=4) as executor:
        digests = list(executor.map(store_thinking, [raw] * 8))
    assert len(set(digests)) == 1
    with pytest.raises(ThinkingReplayError, match="full"):
        store_thinking(b'{"different":true}')
    assert load_thinking(digests[0]) == json.loads(raw)
    monkeypatch.setattr("agentroute.claude_thinking_store.MAX_BLOCK_BYTES", 4)
    with pytest.raises(ThinkingReplayError, match="limit"):
        store_thinking(raw)


def test_storage_failure_fails_stream_before_emitting_reference(store_path, monkeypatch):
    def unavailable(_raw):
        raise ThinkingReplayError("Claude thinking replay storage is unavailable.")

    monkeypatch.setattr("agentroute.claude_bridge.store_thinking", unavailable)
    stream = ResponsesStream("resp_failed", "claude-sonnet-5-5")
    stream.feed(
        "content_block_start",
        {
            "index": 0,
            "content_block": {
                "type": "thinking",
                "thinking": "x" * 8_000,
            },
        },
    )
    event = json.loads(stream.feed("content_block_stop", {"index": 0})[0].split(b"data: ", 1)[1])
    assert event["type"] == "response.failed"
    assert "unavailable" in event["response"]["error"]["message"]
    assert stream.items == []
    assert stream.feed("message_stop", {}) == []
