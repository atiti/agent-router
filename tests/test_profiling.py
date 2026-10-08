import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from agentroute.config import default_config
from agentroute.context_profile import (
    capture_enabled,
    normalize_anthropic_usage,
    record_bridge_profile,
    request_identity,
    summarize_context,
)
from agentroute.profile_cli import app
from agentroute.profiling import (
    capture_status,
    price_usage,
    profile_report,
    read_classifier_receipts,
    read_profiles,
    read_rollout_receipts,
    read_traces,
    set_capture,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path))
    monkeypatch.delenv("AGENTROUTE_PROFILE_CAPTURE", raising=False)
    return tmp_path


def test_anthropic_input_includes_both_cache_buckets_without_double_counting():
    usage = normalize_anthropic_usage(
        {
            "input_tokens": 10,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 200,
            "output_tokens": 30,
            "cache_creation": {"ephemeral_5m_input_tokens": 150, "ephemeral_1h_input_tokens": 50},
        }
    )
    assert usage["input_tokens"] == 1210
    assert usage["uncached_input_tokens"] == 10
    assert usage["total_tokens"] == 1240
    assert usage["cache_write_1h_input_tokens"] == 50
    assert (
        normalize_anthropic_usage({"input_tokens": True, "output_tokens": float("nan")})[
            "total_tokens"
        ]
        == 0
    )


def test_context_sections_are_measured_utf8_and_do_not_retain_prompt_text():
    skill = "<skill><name>ati-cto</name>secret skill body</skill>"
    catalog = "<skills_instructions>secret catalog</skills_instructions>"
    agents = "# AGENTS.md instructions for /project\n<INSTRUCTIONS>secret rules</INSTRUCTIONS>"
    memory = (
        "## Memory\nguidance\n========= MEMORY_SUMMARY BEGINS =========secret memory"
        "========= MEMORY_SUMMARY ENDS ========="
    )
    result = summarize_context(
        {
            "instructions": "héllo",
            "input": [
                {"type": "message", "role": "developer", "content": catalog + "\n" + memory},
                {"role": "user", "content": skill + agents},
                {
                    "type": "function_call",
                    "call_id": "call1",
                    "name": "exec_command",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call1",
                    "output": "private tool result",
                },
                {"type": "reasoning", "encrypted_content": "opaque-secret"},
            ],
            "tools": [{"name": "exec_command", "description": "private definition"}],
        }
    )
    parts = result["components"]
    assert parts["base_instructions"]["bytes"] == len("héllo".encode())
    for name, value in (
        ("loaded_skills", skill),
        ("skills_catalog", catalog),
        ("agents_instructions", agents),
        ("memory", memory),
    ):
        assert parts[name]["bytes"] == len(value.encode())
    assert result["media"]["opaque_state_bytes"] == len("opaque-secret")
    named = {(p["category"], p["name"]) for p in result["largest_named_items"]}
    assert ("loaded_skills", "ati-cto") in named
    assert ("tool_results", "exec_command") in named
    serialized = json.dumps(result)
    assert "secret" not in serialized and "private" not in serialized
    assert result["cache_control_present"] is False


def test_anthropic_tool_results_and_media_are_not_mistaken_for_user_text():
    result = summarize_context(
        {
            "system": [{"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}],
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "t1", "name": "look", "input": {}}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [
                                {"type": "text", "text": "result"},
                                {"type": "image", "source": {"data": "A" * 1000}},
                            ],
                        }
                    ],
                },
            ],
        },
        anthropic=True,
    )
    assert result["components"]["tool_results"]["bytes"] == 6
    assert result["media"]["images"] == 1
    assert result["estimated_text_tokens"] < 100
    assert result["cache_control_present"]


def test_capture_marker_and_sidecar_permissions(home):
    assert not capture_enabled()
    set_capture(True)
    assert capture_enabled()
    assert (home / "profiling").stat().st_mode & 0o777 == 0o700
    record_bridge_profile({"source": "anthropic_bridge", "version": 1, "usage": None})
    path = next((home / "profiling" / "bridge").rglob("*.json"))
    assert path.stat().st_mode & 0o777 == 0o600
    assert capture_status()["bridge_receipts"] == 1
    set_capture(False)
    assert not capture_enabled() and path.is_file()


def test_identity_does_not_retain_arbitrary_metadata():
    result = request_identity(
        {
            "x-codex-turn-metadata": json.dumps({"turn_id": "t", "secret": "no"}),
            "session-id": "s",
            "x-codex-inference-call-id": "inference-1",
        },
        {"client_metadata": {"thread_id": "th", "secret": "no"}},
    )
    assert result["turn_id"] == "t" and result["session_id"] == "s"
    assert result["inference_call_id"] == "inference-1"
    assert "secret" not in json.dumps(result)
    assert request_identity({"session-id": "text\nnot an ID"}, {})["session_id"] is None


def make_bundle(root, specs, *, events_extra=()):
    bundle = root / "traces" / "bundle"
    bundle.mkdir(parents=True)
    events = []
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for index, (call, request, response) in enumerate(specs):
        for suffix, data in (("request", request), ("response", response)):
            (bundle / f"{call}-{suffix}.json").write_text(json.dumps(data))
        events.append(
            {
                "wall_time_unix_ms": now_ms + index,
                "rollout_id": "s",
                "thread_id": "th",
                "codex_turn_id": "t",
                "payload": {
                    "type": "inference_started",
                    "inference_call_id": call,
                    "model": "gpt-6-sol",
                    "provider_name": "openai",
                    "thread_id": "th",
                    "codex_turn_id": "t",
                    "request_payload": {"path": f"{call}-request.json"},
                },
            }
        )
        events.append(
            {
                "wall_time_unix_ms": now_ms + index,
                "payload": {
                    "type": "inference_completed",
                    "inference_call_id": call,
                    "response_id": response.get("response_id"),
                    "response_payload": {"path": f"{call}-response.json"},
                },
            }
        )
    events.extend(events_extra)
    (bundle / "trace.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
    return bundle


def test_native_trace_reconstructs_websocket_history_and_counts_usage_per_response(home):
    root = home / "profiling"
    make_bundle(
        root,
        [
            (
                "i1",
                {"instructions": "rules", "input": [{"role": "user", "content": "first"}]},
                {
                    "response_id": "r1",
                    "token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 80,
                        "output_tokens": 10,
                    },
                    "output_items": [{"role": "assistant", "content": "answer"}],
                },
            ),
            (
                "i2",
                {"previous_response_id": "r1", "input": [{"role": "user", "content": "second"}]},
                {"response_id": "r2", "token_usage": {"input_tokens": 120, "output_tokens": 20}},
            ),
        ],
    )
    rows, errors = read_traces(root / "traces")
    assert errors == 0
    assert rows[1]["context_coverage"] == "reconstructed_websocket"
    assert rows[1]["context"]["components"]["user_messages"]["bytes"] == 11
    assert rows[1]["context"]["components"]["assistant_history"]["bytes"] == 6
    assert rows[1]["context"]["components"]["base_instructions"]["bytes"] == 5
    report = profile_report(default_config().pricing, root=root)
    assert report["tokens"]["input_tokens"] == 220
    assert report["tokens"]["uncached_input_tokens"] == 140
    assert report["tokens"]["output_tokens"] == 30
    assert report["context_exposure_bytes"]["base_instructions"] == 10
    assert "answer" not in json.dumps(report)
    assert (
        profile_report(default_config().pricing, root=root, session="foreign")["requests_count"]
        == 0
    )


def test_missing_ws_prefix_and_corruption_are_reported_without_failure(home):
    root = home / "profiling"
    bundle = make_bundle(
        root, [("i1", {"previous_response_id": "missing", "input": []}, {"token_usage": None})]
    )
    with (bundle / "trace.jsonl").open("a") as f:
        f.write('{"partial"')
    rows, errors = read_traces(root / "traces")
    assert errors == 1 and len(rows) == 1
    assert rows[0]["context_coverage"].startswith("partial")
    assert rows[0]["usage"] is None


def test_trace_payload_path_cannot_escape_bundle(home):
    root = home / "profiling"
    bundle = make_bundle(root, [("i1", {}, {})])
    data = (bundle / "trace.jsonl").read_text().replace("i1-request.json", "../../secret.json")
    (bundle / "trace.jsonl").write_text(data)
    rows, errors = read_traces(root / "traces")
    assert rows == [] and errors == 2


def test_bridge_receipt_replaces_native_receipt_once_and_keeps_native_ids(home):
    root = home / "profiling"
    make_bundle(
        root,
        [
            (
                "i1",
                {"input": []},
                {"response_id": "r1", "token_usage": {"input_tokens": 3, "output_tokens": 4}},
            )
        ],
    )
    record_bridge_profile(
        {
            "version": 1,
            "source": "anthropic_bridge",
            "response_id": "r1",
            "thread_id": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": "claude-opus-5-5",
            "outcome": "completed",
            "usage": normalize_anthropic_usage(
                {"input_tokens": 10, "cache_read_input_tokens": 100, "output_tokens": 20}
            ),
            "context": summarize_context(
                {"messages": [{"role": "user", "content": "outbound"}]}, anthropic=True
            ),
        }
    )
    rows, errors = read_profiles(root)
    assert errors == 0 and len(rows) == 1
    assert rows[0]["thread_id"] == "th"
    assert rows[0]["source"] == "codex_trace+anthropic_bridge"
    report = profile_report(default_config().pricing, root=root)
    assert report["tokens"]["input_tokens"] == 110
    assert report["tokens"]["output_tokens"] == 20


def test_api_equivalent_cost_splits_cache_ttl_and_output(home):
    usage = normalize_anthropic_usage(
        {
            "input_tokens": 100_000,
            "cache_read_input_tokens": 500_000,
            "cache_creation_input_tokens": 300_000,
            "output_tokens": 10_000,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 200_000,
                "ephemeral_1h_input_tokens": 100_000,
            },
        }
    )
    cost = price_usage({"model": "claude-opus-5-5", "usage": usage}, default_config().pricing)
    assert cost["total_usd"] == pytest.approx(0.4 + 0.1 + 1 + 0.8 + 0.2)
    assert cost["assumptions"] == []
    assert "subscription" not in cost["basis"]


def test_haiku55_profile_cost_uses_long_context_multiplier():
    pricing = default_config().pricing
    usage = {"input_tokens": 100_001, "output_tokens": 1000}
    report = price_usage({"model": "claude-haiku-5-5", "usage": usage}, pricing)
    assert report["total_usd"] == pytest.approx((100_001 * 0.10 + 1000 * 0.50) * 5 / 1_000_000)


def test_unknown_prices_and_ttl_are_explicit_and_zero_write_price_is_respected():
    pricing = default_config().pricing
    assert not price_usage({"model": "unknown", "usage": {}}, pricing)["priced"]
    price = pricing.models["claude-opus-5-5"]
    price.cache_write_per_million = 0
    usage = {"input_tokens": 10, "cache_write_input_tokens": 10}
    result = price_usage({"model": "claude-opus-5-5", "usage": usage}, pricing)
    assert result["total_usd"] == 0 and result["assumptions"]
    price.cache_write_1h_per_million = None
    usage["cache_write_1h_input_tokens"] = 10
    assert not price_usage({"model": "claude-opus-5-5", "usage": usage}, pricing)["priced"]


def test_classifier_database_is_read_only_and_no_raw_receipt_is_exported(home):
    path = home / "audit.db"
    with sqlite3.connect(path) as c:
        c.execute(
            "CREATE TABLE routing_decisions(id, created_at, session_id, turn_id, "
            "classifier_usage, selection_receipt)"
        )
        c.execute(
            "INSERT INTO routing_decisions VALUES(?,?,?,?,?,?)",
            (
                1,
                datetime.now(timezone.utc).isoformat(),
                "s",
                "t",
                json.dumps({"prompt_tokens": 100, "completion_tokens": 5}),
                json.dumps({"classifier": {"model": "jev-latest", "raw_text": "secret"}}),
            ),
        )
    before = path.read_bytes()
    result = profile_report(default_config().pricing, root=home / "profiling")
    assert result["requests_count"] == 1
    assert result["tokens"]["input_tokens"] == 100
    assert result["priced_usd"] == pytest.approx(0.0000042)
    assert "secret" not in json.dumps(result) and path.read_bytes() == before
    missing = home / "missing.db"
    assert read_classifier_receipts(missing, datetime.now(timezone.utc)) == ([], 0)
    assert not missing.exists()


def test_untraced_rollout_receipts_use_response_usage_never_cumulative_totals(home):
    now = datetime.now(timezone.utc)
    path = home / "rollout.jsonl"
    payload = {
        "response_id": "r1",
        "thread_id": "th",
        "turn_id": "t",
        "usage": {"input_tokens": 100, "output_tokens": 3},
        "turn_token_usage": {"input_tokens": 5000},
    }
    line = json.dumps(
        {"type": "token_usage_record", "timestamp": now.isoformat(), "payload": payload}
    )
    path.write_text(line + "\n" + line + "\n")
    rows, errors = read_rollout_receipts(path)
    assert errors == 0 and len(rows) == 1
    assert rows[0]["usage"]["input_tokens"] == 100
    assert rows[0]["model"] == "unknown_untraced_model"
    rows, _ = read_rollout_receipts(path, int((now + timedelta(seconds=1)).timestamp() * 1000))
    assert rows == []


def test_profile_commands_and_private_json_export(home):
    runner = CliRunner()
    assert runner.invoke(app, ["on"]).exit_code == 0
    assert capture_enabled()
    result = runner.invoke(app, ["status"])
    assert json.loads(result.stdout)["enabled"]
    assert runner.invoke(app, ["off"]).exit_code == 0
    assert not capture_enabled()
    output = home / "report.json"
    result = runner.invoke(app, ["report", "--json", "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["requests_count"] == 0
    assert output.stat().st_mode & 0o777 == 0o600


def test_associated_rollout_recovers_untraced_usage_and_deduplicates_known_response(home):
    root = home / "profiling"
    bundle = make_bundle(
        root,
        [
            (
                "i1",
                {"instructions": "rules", "input": []},
                {"response_id": "r1", "token_usage": {"input_tokens": 100, "output_tokens": 2}},
            )
        ],
    )
    log = bundle / "trace.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    start = events[0]["wall_time_unix_ms"] - 10
    rollout = home / "rollout.jsonl"
    receipts = []
    for response_id, amount in (("r1", 100), ("compaction-response", 200)):
        receipts.append(
            {
                "type": "token_usage_record",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": {
                    "response_id": response_id,
                    "thread_id": "th",
                    "session_id": "s",
                    "turn_id": "t",
                    "usage": {"input_tokens": amount, "output_tokens": 2},
                },
            }
        )
    rollout.write_text("".join(json.dumps(receipt) + "\n" for receipt in receipts))
    (bundle / "metadata.json").write_text(json.dumps({"rollout_path": str(rollout)}))
    events.insert(
        0,
        {
            "wall_time_unix_ms": start,
            "thread_id": "th",
            "payload": {
                "type": "thread_started",
                "thread_id": "th",
                "agent_path": "/root",
                "metadata_payload": {"path": "metadata.json"},
            },
        },
    )
    log.write_text("".join(json.dumps(event) + "\n" for event in events))
    report = profile_report(default_config().pricing, root=root)
    assert report["read_errors"] == 0
    assert report["requests_count"] == 2
    assert report["tokens"]["input_tokens"] == 300
    assert report["tokens"]["output_tokens"] == 4
    assert report["unpriced_requests"] == 1
    assert report["largest_context_request"]["response_id"] == "r1"
    assert report["threads"][0]["agent_path"] == "/root"


def test_unmeasured_compaction_request_has_context_and_explicit_missing_usage(home):
    root = home / "profiling"
    bundle = make_bundle(root, [("i1", {"instructions": "summarize", "input": []}, {})])
    log = bundle / "trace.jsonl"
    content = (
        log.read_text()
        .replace("inference_started", "compaction_request_started")
        .replace("inference_completed", "compaction_request_completed")
        .replace("inference_call_id", "compaction_request_id")
    )
    log.write_text(content)
    report = profile_report(default_config().pricing, root=root)
    assert report["requests_count"] == 1 and report["measured_requests"] == 0
    assert report["requests"][0]["purpose"] == "compaction"
    assert report["unpriced_requests"] == 1
    assert (
        report["largest_context_request"]["context"]["components"]["base_instructions"]["bytes"]
        == 9
    )


def test_terminal_report_with_measured_data_and_quota_samples(home):
    record_bridge_profile(
        {
            "version": 1,
            "source": "anthropic_bridge",
            "response_id": "r1",
            "thread_id": "th",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": "claude-opus-5-5",
            "outcome": "completed",
            "context_window": 200_000,
            "usage": normalize_anthropic_usage({"input_tokens": 100, "output_tokens": 5}),
            "context": summarize_context(
                {"messages": [{"role": "user", "content": "hello"}]}, anthropic=True
            ),
            "subscription_sample_at": 1000,
            "subscription_windows": {"five_hour": {"used_percent": 50, "resets_at": 2000}},
        }
    )
    result = CliRunner().invoke(app, ["report"])
    assert result.exit_code == 0, result.output
    assert "Fresh input" in result.output and "Observed account quota" in result.output
    assert "hello" not in result.output


def test_json_schema_properties_named_type_cannot_break_context_capture():
    payload = {
        "input": [{"type": "function_call", "call_id": "t1", "name": "task", "arguments": "{}"}],
        "tools": [
            {
                "type": "function",
                "name": "task",
                "parameters": {
                    "type": "object",
                    "properties": {"type": {"type": ["string", "null"]}},
                },
            }
        ],
    }
    result = summarize_context(payload)
    assert result["tool_count"] == 1
    assert result["components"]["tool_definitions"]["bytes"] > 0
    result = summarize_context(
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "task",
                            "input": {
                                "type": {"type": "string"},
                            },
                        }
                    ],
                }
            ]
        },
        anthropic=True,
    )
    assert result["components"]["tool_calls"]["bytes"] > 0


def test_classifier_only_report_does_not_claim_captured_context(home):
    with sqlite3.connect(home / "audit.db") as connection:
        connection.execute(
            "CREATE TABLE routing_decisions(id, created_at, session_id, turn_id, "
            "classifier_usage, selection_receipt)"
        )
        connection.execute(
            "INSERT INTO routing_decisions VALUES(?,?,?,?,?,?)",
            (
                1,
                datetime.now(timezone.utc).isoformat(),
                "s",
                "t",
                json.dumps({"prompt_tokens": 100}),
                json.dumps({"classifier": {"model": "local-unpriced"}}),
            ),
        )
    result = CliRunner().invoke(app, ["report"])
    assert result.exit_code == 0, result.output
    assert "No assembled request captures yet" in result.output
    assert "Largest captured context" not in result.output
    assert "unknown" in result.output
