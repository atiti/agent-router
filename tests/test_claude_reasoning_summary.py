import base64
import json

from agentroute.claude_bridge import REASONING_ITEM_PREFIX, ResponsesStream


def test_reasoning_preview_streams_early_is_bounded_and_preserves_signed_replay():
    stream = ResponsesStream("resp_preview", "claude-sonnet-5-5")
    stream.feed("message_start", {"message": {}})
    start = stream.feed(
        "content_block_start",
        {
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
    )
    assert b"response.output_item.added" in b"".join(start)
    full_text = "checking évidence " * 100
    deltas = stream.feed(
        "content_block_delta",
        {
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": full_text},
        },
    )
    assert b"response.reasoning_summary_text.delta" in b"".join(deltas)
    stream.feed(
        "content_block_delta",
        {
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "signed-provider-data"},
        },
    )
    done = stream.feed("content_block_stop", {"index": 0})
    item = stream.items[0]
    assert len(item["summary"][0]["text"].encode()) <= 512
    assert full_text.startswith(item["summary"][0]["text"])
    assert b"response.output_item.added" not in b"".join(done)
    assert b"response.output_item.done" in b"".join(done)
    encoded = item["encrypted_content"].removeprefix(REASONING_ITEM_PREFIX)
    block = json.loads(base64.urlsafe_b64decode(encoded))["block"]
    assert block == {"type": "thinking", "thinking": full_text, "signature": "signed-provider-data"}
