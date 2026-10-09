import copy
import json
import sqlite3
import time

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from agentroute.claude_bridge import translate_request
from agentroute.claude_cache_cli import app
from agentroute.claude_cache_usage import cache_usage_report, record_cache_usage
from agentroute.config import ClaudeCacheConfig, default_config, load_config, save_config


def test_cache_boundaries_preserve_transcript_and_tool_continuations():
    body = {
        "instructions": "unchanging rules",
        "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "question"}],
            },
            {"type": "function_call", "name": "lookup", "call_id": "call", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call", "output": "result"},
        ],
    }
    original = copy.deepcopy(body)
    plain, _, _ = translate_request(body, cache_config=ClaudeCacheConfig(mode="off"))
    cached, _, _ = translate_request(body, cache_config=ClaudeCacheConfig(ttl="1h"))
    assert cached.pop("cache_control") == {"type": "ephemeral", "ttl": "1h"}
    for section in ("tools", "system"):
        assert cached[section][-1].pop("cache_control") == {"type": "ephemeral", "ttl": "1h"}
    assert cached == plain
    assert body == original
    # Adjacent user blocks merge, preserving the existing content as a prefix.
    body["input"].append(
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "next question"}],
        }
    )
    continued, _, _ = translate_request(body)
    assert continued["messages"][:-1] == plain["messages"][:-1]
    last_content = plain["messages"][-1]["content"]
    assert continued["messages"][-1]["role"] == plain["messages"][-1]["role"]
    assert continued["messages"][-1]["content"][: len(last_content)] == last_content


def test_cache_cli_persists_valid_settings_and_does_not_enable_raw_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path))
    save_config(default_config())
    runner = CliRunner()
    assert runner.invoke(app, ["configure", "auto", "--ttl", "1h"]).exit_code == 0
    assert load_config().claude_cache == ClaudeCacheConfig(ttl="1h")
    assert runner.invoke(app, ["configure", "off"]).exit_code == 0
    assert load_config().claude_cache.mode == "off"
    assert not (tmp_path / "profiling" / "enabled").exists()
    assert runner.invoke(app, ["configure", "auto", "--ttl", "forever"]).exit_code != 0
    with pytest.raises(ValidationError):
        ClaudeCacheConfig(ttl="forever")


def test_cache_receipts_distinguish_profiles_models_and_unknown_usage(tmp_path):
    path = tmp_path / "cache.sqlite3"
    for profile, model, usage in (
        (
            "default",
            "claude-haiku-5-5",
            {
                "input_tokens": 120,
                "cached_input_tokens": 100,
                "cache_write_input_tokens": 15,
                "cache_write_5m_input_tokens": 15,
            },
        ),
        ("second", "claude-haiku-5-5", {"input_tokens": 50}),
        ("second", "claude-sonnet-5-5", None),
    ):
        record_cache_usage(
            profile=profile,
            model=model,
            mode="auto",
            ttl="5m",
            outcome="completed",
            usage=usage,
            boundaries={"tools_bytes": 42},
            path=path,
        )
    report = cache_usage_report(path=path)
    groups = {(r["profile"], r["model"]): r for r in report["groups"]}
    default = groups[("default", "claude-haiku-5-5")]
    assert default["tokens"]["uncached_input_tokens"] == 5
    assert default["cache_read_percent"] == pytest.approx(100 * 100 / 120)
    assert groups[("second", "claude-haiku-5-5")]["tokens"]["cached_input_tokens"] == 0
    unknown = groups[("second", "claude-sonnet-5-5")]
    assert (unknown["measured_requests"], unknown["cache_read_percent"]) == (0, None)
    assert default["forwarded_prefix_bytes"] == {"tools_bytes": 42, "system_bytes": 0}
    if path.stat().st_mode & 0o777:  # Windows does not implement POSIX modes.
        import os

        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600


def test_cache_receipts_are_bounded_and_fail_open(tmp_path, monkeypatch):
    import agentroute.claude_cache_usage as receipts

    monkeypatch.setattr(receipts, "RECEIPT_LIMIT", 2)
    path = tmp_path / "cache.sqlite3"
    kwargs = dict(
        profile="default",
        model="claude-haiku-5-5",
        mode="auto",
        ttl="5m",
        outcome="failed",
        usage=None,
        boundaries={},
        path=path,
    )
    for _ in range(3):
        record_cache_usage(**kwargs)
    assert cache_usage_report(path=path)["retained_requests"] == 2
    with sqlite3.connect(path) as db:
        db.execute("UPDATE receipts SET recorded_at = ?", (time.time() - 31 * 86400,))
    record_cache_usage(**kwargs)
    assert cache_usage_report(path=path)["retained_requests"] == 1
    # A locked or broken telemetry store must not block a model response.
    with sqlite3.connect(path) as db:
        db.execute("BEGIN EXCLUSIVE")
        record_cache_usage(**kwargs)
    record_cache_usage(**{**kwargs, "usage": {"input_tokens": 2**100}})
    assert cache_usage_report(path=path)["retained_requests"] == 1
    path.write_text("not a database")
    record_cache_usage(**kwargs)
    assert cache_usage_report(path=path)["error"]


def test_cache_status_reports_counts_without_payloads(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path))
    record_cache_usage(
        profile="second",
        model="claude-haiku-5-5",
        mode="auto",
        ttl="1h",
        outcome="completed",
        usage={"input_tokens": 1000, "cached_input_tokens": 900},
        boundaries={"tools_bytes": 500},
    )
    runner = CliRunner()
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    group = json.loads(result.stdout)["groups"][0]
    assert (group["profile"], group["cache_read_percent"]) == ("second", 90.0)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "second" in result.stdout and "90.0%" in result.stdout
