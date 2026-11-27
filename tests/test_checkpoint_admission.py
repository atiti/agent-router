import json
from unittest.mock import patch

import pytest
from test_hook import invoke

from agentroute.audit import AuditStore
from agentroute.capacity import CapacityState
from agentroute.checkpoints import Checkpoint, latest_checkpoint
from agentroute.config import default_config
from agentroute.profiles import ProfileStatus, TurnProfileSelection, account_hash


def write_checkpoint(path, provider="openai", *, portable=False):
    items = (
        [{"type": "message", "role": "user", "content": []}]
        if portable
        else [{"type": "compaction", "id": "cmp_test"}]
    )
    path.write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {"type": "session_meta", "payload": {"id": "same-thread"}},
                {
                    "type": "compacted",
                    "timestamp": "2026-01-01T01:00:00Z",
                    "payload": {
                        "replacement_history": items,
                        "replacement_history_metadata": [{"model_provider_id": provider}],
                    },
                },
                {"type": "token_usage_record", "payload": {"response_id": "resp_verified"}},
            ]
        )
        + "\n"
    )


def setup_route(tmp_path):
    config = default_config()
    config.enabled = True
    config.routing.mode = "heuristic"
    config.capacity.enabled = False
    store = AuditStore(tmp_path / "audit.db")
    invoke(config, store, "@gpt @max explain this", account_id="test-account")
    with store.connection() as db:
        db.execute(
            "UPDATE routing_decisions SET created_at='2026-01-01T00:00:00+00:00', "
            "actual_model_provider='openai', route_application_state='applied', "
            "turn_outcome='completed', capacity_account_id=?",
            (account_hash("test-account"),),
        )
    path = tmp_path / "rollout.jsonl"
    write_checkpoint(path)
    status = ProfileStatus(
        "default",
        str(tmp_path),
        0,
        True,
        True,
        account_hash("test-account"),
        CapacityState("gpt", "healthy", "test"),
    )
    selection = TurnProfileSelection("default", status, status, False, "account_match", False)
    return config, store, path, selection


def test_opaque_history_reader_uses_latest_replacement_and_thread_identity(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_checkpoint(path)
    assert latest_checkpoint(str(path), "same-thread") == Checkpoint(
        "openai", "2026-01-01T01:00:00+00:00"
    )
    assert latest_checkpoint(str(path), "other-thread") == Checkpoint(None, "")
    with path.open("a") as f:
        f.write(
            json.dumps(
                {
                    "type": "compacted",
                    "timestamp": "2026-01-01T02:00:00Z",
                    "payload": {
                        "replacement_history": [{"type": "message", "role": "user", "content": []}]
                    },
                }
            )
            + "\n"
        )
    assert latest_checkpoint(str(path), "same-thread") is None


def test_mixed_checkpoint_requests_recovery_without_clearing_guard(tmp_path):
    config, store, path, selection = setup_route(tmp_path)
    # This is the observed legacy failure: the last attempted route latched stripping.
    with store.connection() as db:
        db.execute("UPDATE routing_decisions SET strip_provider_state=1")
    with patch("agentroute.hook.select_turn_profile", return_value=selection):
        result = invoke(
            config,
            store,
            "@gpt @max continue",
            transcript_path=path,
            account_id="test-account",
            model_provider="agentroute-azure",
        )
    assert result["continue"] is False
    assert "compact to plaintext" in result["stopReason"]
    assert store.latest("same-thread")["strip_provider_state"] == 1


@pytest.mark.parametrize("prompt", ["@azure @max continue", "@gpt @max continue", "@auto continue"])
def test_opaque_checkpoint_is_held_without_switch_or_account_inference(tmp_path, prompt):
    config, store, path, selection = setup_route(tmp_path)
    before = store.latest("same-thread")["id"]
    with patch("agentroute.hook.select_turn_profile", return_value=selection):
        result = invoke(config, store, prompt, transcript_path=path, account_id="test-account")
    assert result["continue"] is False
    assert "compact to plaintext" in result["stopReason"]
    assert store.latest("same-thread")["id"] == before
    assert "hookSpecificOutput" not in result


def test_plaintext_replacement_releases_checkpoint_constraint(tmp_path):
    config, store, path, selection = setup_route(tmp_path)
    write_checkpoint(path, portable=True)
    with patch("agentroute.hook.select_turn_profile", return_value=selection):
        result = invoke(
            config, store, "@gpt @max continue", transcript_path=path, account_id="test-account"
        )
    assert result["continue"] is True
    assert "CHECKPOINT OWNER RETAINED" not in result["hookSpecificOutput"]["routeMessage"]


def test_reader_ignores_partial_final_write(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_checkpoint(path)
    with path.open("a") as f:
        f.write('{"type": "event_msg", "payload":')
    assert latest_checkpoint(str(path), "same-thread") == Checkpoint(None, "")


def test_missing_or_malformed_history_is_unknown(tmp_path):
    missing = tmp_path / "missing.jsonl"
    assert latest_checkpoint(str(missing), "same-thread") == Checkpoint(None, "")
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {"type": "session_meta", "payload": {"id": "same-thread"}},
                {"type": "compacted", "timestamp": "2026-01-01T01:00:00Z", "payload": {}},
            ]
        )
        + "\n"
    )
    assert latest_checkpoint(str(path), "same-thread") == Checkpoint(None, "")


def test_bounded_scan_cannot_claim_older_checkpoint_absent(tmp_path, monkeypatch):
    path = tmp_path / "rollout.jsonl"
    write_checkpoint(path)
    with path.open("a") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"padding": "x" * 1024}}) + "\n")
    monkeypatch.setattr("agentroute.checkpoints.MAX_SCAN_BYTES", 128)
    assert latest_checkpoint(str(path), "same-thread") == Checkpoint(None, "")


def test_malformed_complete_record_and_replacement_are_unknown(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_checkpoint(path, portable=True)
    with path.open("a") as f:
        f.write("invalid json\n")
    assert latest_checkpoint(str(path), "same-thread") == Checkpoint(None, "")
    path.write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {"type": "session_meta", "payload": {"id": "same-thread"}},
                {
                    "type": "compacted",
                    "timestamp": "2026-01-01T01:00:00Z",
                    "payload": {"replacement_history": [{}]},
                },
            ]
        )
        + "\n"
    )
    assert latest_checkpoint(str(path), "same-thread") == Checkpoint(None, "")


def test_encrypted_portable_history_is_not_assumed_safe(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {"type": "session_meta", "payload": {"id": "same-thread"}},
                {
                    "type": "compacted",
                    "timestamp": "2026-01-01T01:00:00Z",
                    "payload": {
                        "replacement_history": [{"type": "message", "encrypted_content": "opaque"}]
                    },
                },
            ]
        )
        + "\n"
    )
    assert latest_checkpoint(str(path), "same-thread") == Checkpoint(None, "")


def test_empty_and_malformed_message_history_is_unknown(tmp_path):
    path = tmp_path / "rollout.jsonl"
    for history in ([], [{"type": "message"}], [{"type": "reasoning"}]):
        path.write_text(
            "\n".join(
                json.dumps(x)
                for x in [
                    {"type": "session_meta", "payload": {"id": "same-thread"}},
                    {
                        "type": "compacted",
                        "timestamp": "2026-01-01T01:00:00Z",
                        "payload": {"replacement_history": history},
                    },
                ]
            )
            + "\n"
        )
        assert latest_checkpoint(str(path), "same-thread") == Checkpoint(None, "")


def test_subagent_uses_runtime_transcript_path_without_child_field(tmp_path):
    config, store, path, selection = setup_route(tmp_path)
    with patch("agentroute.hook.select_turn_profile", return_value=selection):
        result = invoke(
            config,
            store,
            "@gpt @max continue",
            transcript_path=path,
            account_id="test-account",
            flat_agent_id="child",
        )
    assert result["continue"] is False
    assert "compact to plaintext" in result["stopReason"]
