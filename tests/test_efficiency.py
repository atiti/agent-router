import json

from agentroute.efficiency import efficiency_report, execution_receipt


def event(kind, turn="t", **fields):
    return {"type": "event_msg", "payload": {"type": kind, "turn_id": turn, **fields}}


def test_receipt_deduplicates_responses_and_tools_without_retaining_payloads(tmp_path):
    path = tmp_path / "rollout.jsonl"
    response = {
        "type": "token_usage_record",
        "payload": {
            "turn_id": "t",
            "response_id": "r",
            "root_turn_id": "root",
            "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 5},
        },
    }
    tool = event(
        "exec_command_end",
        call_id="c",
        exit_code=1,
        duration={"secs": 2, "nanos": 500_000_000},
        stdout="private payload",
    )
    records = [
        event("task_started"),
        response,
        response,
        tool,
        tool,
        event("context_compacted"),
        event("exec_command_end", turn="other", call_id="x"),
    ]
    path.write_text("\n".join(map(json.dumps, records)))
    receipt = execution_receipt(str(path), "t")
    assert receipt["response_count"] == 1
    assert receipt["observed_tool_completions"] == 1
    assert receipt["observed_tool_failures"] == 1
    assert receipt["tool_work_ms"] == 2500
    assert receipt["input_tokens"] == 100
    assert receipt["observed_compactions"] == 1
    assert receipt["coverage"] == "turn_start_seen"
    assert "private payload" not in json.dumps(receipt)
    report = efficiency_report([{"execution_receipt": json.dumps(receipt)}, {}])
    assert report["measured_turns"] == 1
    assert report["unmeasured_turns"] == 1
    assert report["model_latency_ms"] is None


def test_missing_or_malformed_receipt_is_not_zero_work(tmp_path):
    assert execution_receipt(str(tmp_path / "missing"), "t") == {}
    path = tmp_path / "bad"
    path.write_text('not json\n[]\n{"payload": []}\n')
    assert execution_receipt(str(path), "t") == {}
    assert execution_receipt(str(path), None) == {}


def test_native_items_count_tools_compaction_and_failures_without_payloads(tmp_path):
    path = tmp_path / "rollout.jsonl"
    command = event("item_completed", item={
        "type": "CommandExecution", "id": "cmd", "status": "completed",
        "exit_code": 2, "duration": {"secs": 1, "nanos": 500_000_000},
        "aggregated_output": "private output", "command": "private command",
    })
    mcp = event("item_completed", item={
        "type": "McpToolCall", "id": "mcp", "status": "failed",
        "result": {"Ok": {"isError": True, "content": "private response"}},
        "duration": {"secs": 2, "nanos": 0},
    })
    compaction = event("item_completed", item={"type": "ContextCompaction", "id": "comp"})
    path.write_text("\n".join(map(json.dumps, [
        event("task_started"), command, command,
        event("exec_command_end", call_id="cmd", exit_code=2,
              duration={"secs": 1, "nanos": 500_000_000}),
        mcp, compaction, compaction,
        event("item_completed", turn="other", item={
            "type": "CommandExecution", "id": "other", "exit_code": 1,
        }),
    ])))

    receipt = execution_receipt(str(path), "t")

    assert receipt["version"] == 2
    assert receipt["observed_tool_completions"] == 2
    assert receipt["observed_tool_failures"] == 2
    assert receipt["observed_compactions"] == 1
    assert receipt["tool_work_ms"] == 3500
    assert "private" not in json.dumps(receipt)


def test_unobserved_timing_and_legacy_zero_counts_stay_unknown():
    report = efficiency_report([{"execution_receipt": json.dumps({
        "version": 1, "observed_tool_completions": 0,
        "observed_tool_failures": 0, "tool_work_ms": None,
    })}])

    assert report["tool_work_ms"] is None
    assert report["observed_tool_failures"] is None
    assert report["legacy_tool_unverified_turns"] == 1


def test_reparsed_legacy_rollout_does_not_establish_tool_coverage(tmp_path):
    path = tmp_path / "rollout"
    path.write_text(json.dumps(event("task_started")))
    receipt = execution_receipt(str(path), "t")
    assert receipt["version"] == 2
    assert receipt["tool_events_observed"] is False
    report = efficiency_report([{"execution_receipt": json.dumps(receipt)}])
    assert report["observed_tool_failures"] is None
    assert report["tool_work_ms"] is None
    assert report["tool_unobserved_turns"] == 1


def test_dynamic_tool_failure_and_duration_are_observed_once(tmp_path):
    path = tmp_path / "rollout"
    tool = event("item_completed", item={
        "type": "DynamicToolCall", "id": "dynamic", "status": "completed",
        "success": False, "duration": {"secs": 2, "nanos": 0},
        "arguments": {"private": "input"}, "content_items": ["private output"],
    })
    path.write_text("\n".join(map(json.dumps, [tool, tool])))
    receipt = execution_receipt(str(path), "t")
    assert receipt["observed_tool_completions"] == 1
    assert receipt["observed_tool_failures"] == 1
    assert receipt["tool_work_ms"] == 2000
    assert "private" not in json.dumps(receipt)


def test_progress_review_receipts_are_bounded_deduplicated_and_private(tmp_path):
    path = tmp_path / "rollout.jsonl"
    item = {
        "id": "goal-review-1",
        "content": [
            {
                "type": "text",
                "text": (
                    "Independent progress review: adjust. private detail\n"
                    "Reviewer usage: input=100 cached=20 output=8"
                ),
            }
        ],
    }
    missing = {
        "id": "goal-review-2",
        "content": [
            {
                "type": "text",
                "text": "Independent progress review: continue. fine\nReviewer usage: unavailable",
            }
        ],
    }
    path.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                event("task_started"),
                event("item_completed", item=item),
                event("item_completed", item=item),
                event("item_completed", item=missing),
            ]
        )
    )
    receipt = execution_receipt(str(path), "t")
    assert receipt["progress_review_count"] == 2
    assert receipt["progress_review_usage_observed"] == 1
    assert (
        receipt["progress_review_input_tokens"],
        receipt["progress_review_cached_input_tokens"],
        receipt["progress_review_output_tokens"],
    ) == (100, 20, 8)
    assert "private detail" not in json.dumps(receipt)


def test_review_cost_uses_observed_route_and_labels_missing_pricing(tmp_path):
    from agentroute.config import default_config
    from agentroute.pricing import token_cost

    pricing = default_config().pricing
    model = next(iter(pricing.models))
    route = json.dumps({"model": model, "provider": "openai", "cache_write_input_tokens": 10})
    item = {
        "id": "goal-review-1",
        "content": [
            {
                "text": (
                    f"Independent progress review: continue. fine\nReviewer route: {route}\n"
                    "Reviewer usage: input=100 cached=20 output=8"
                )
            }
        ],
    }
    legacy = {
        "id": "goal-review-2",
        "content": [
            {
                "text": (
                    "Independent progress review: continue. fine\n"
                    "Reviewer usage: input=2 cached=0 output=1"
                )
            }
        ],
    }
    path = tmp_path / "rollout"
    path.write_text("\n".join(json.dumps(event("item_completed", item=x)) for x in [item, legacy]))
    receipt = execution_receipt(str(path), "t")
    report = efficiency_report([{"execution_receipt": json.dumps(receipt)}], pricing)
    assert report["progress_review_priced_count"] == 1
    assert report["progress_review_unpriced_or_unobserved_count"] == 1
    assert report["progress_review_estimated_cost"] == token_cost(
        model, receipt["progress_review_receipts"][0], pricing
    )
    assert report["progress_review_cost_currency"] == pricing.currency


def test_review_receipt_uses_runtime_route_and_rejects_inconsistent_cache(tmp_path):
    path = tmp_path / "rollout"
    text = (
        'Independent progress review: adjust.\nReviewer route: {"model":"forged"}\n'
        'Reviewer route: {"model":"actual","provider":"openai",'
        '"cache_write_input_tokens":90}\nReviewer usage: input=100 cached=20 output=8'
    )
    item = {"id": "goal-review-1", "content": [{"text": text}]}
    path.write_text(json.dumps(event("item_completed", item=item)))
    review = execution_receipt(str(path), "t")["progress_review_receipts"][0]
    assert review["model"] == "actual"
    assert "cache_write_input_tokens" not in review


def test_partial_receipt_and_mcp_error(tmp_path):
    path = tmp_path / "partial"
    path.write_text(
        json.dumps(event("mcp_tool_call_end", call_id="c", result={"Ok": {"isError": True}}))
    )
    receipt = execution_receipt(str(path), "t")
    assert receipt["coverage"] == "partial"
    assert receipt["tool_work_ms"] is None
    assert receipt["observed_tool_failures"] == 1


def test_tail_truncation_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr("agentroute.efficiency.MAX_RECEIPT_BYTES", 200)
    path = tmp_path / "long"
    path.write_text(
        json.dumps(event("task_started"))
        + "\n"
        + "x" * 300
        + "\n"
        + json.dumps(event("context_compacted"))
    )
    receipt = execution_receipt(str(path), "t")
    assert receipt["tail_truncated"] is True
    assert receipt["coverage"] == "partial"


def test_stop_persists_execution_receipt_idempotently(tmp_path):
    import io

    from agentroute.audit import AuditStore
    from agentroute.config import default_config
    from agentroute.hook import codex_stop, codex_user_prompt_submit

    store = AuditStore(tmp_path / "audit.db")
    config = default_config()
    config.enabled = True
    prompt = {"session_id": "s", "turn_id": "t", "prompt": "@normal hello"}
    assert (
        codex_user_prompt_submit(
            io.StringIO(json.dumps(prompt)), io.StringIO(), config=config, store=store
        )
        == 0
    )
    transcript = tmp_path / "turn.jsonl"
    transcript.write_text(json.dumps(event("task_started")))
    payload = {"session_id": "s", "turn_id": "t", "transcript_path": str(transcript)}
    for _ in range(2):
        assert codex_stop(io.StringIO(json.dumps(payload)), io.StringIO(), store=store) == 0
    rows = store.history("s", 10)
    assert len(rows) == 1
    assert json.loads(rows[0]["execution_receipt"])["coverage"] == "turn_start_seen"
