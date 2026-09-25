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
    assert codex_user_prompt_submit(io.StringIO(json.dumps(prompt)), io.StringIO(),
                                    config=config, store=store) == 0
    transcript = tmp_path / "turn.jsonl"
    transcript.write_text(json.dumps(event("task_started")))
    payload = {"session_id": "s", "turn_id": "t", "transcript_path": str(transcript)}
    for _ in range(2):
        assert codex_stop(io.StringIO(json.dumps(payload)), io.StringIO(), store=store) == 0
    rows = store.history("s", 10)
    assert len(rows) == 1
    assert json.loads(rows[0]["execution_receipt"])["coverage"] == "turn_start_seen"
