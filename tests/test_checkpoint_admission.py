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
    items = [] if portable else [{"type": "compaction", "id": "cmp_test"}]
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
        "openai", "2026-01-01T01:00:00+00:00", True
    )
    assert latest_checkpoint(str(path), "other-thread") is None
    with path.open("a") as f:
        f.write(
            json.dumps(
                {
                    "type": "compacted",
                    "timestamp": "2026-01-01T02:00:00Z",
                    "payload": {"replacement_history": []},
                }
            )
            + "\n"
        )
    assert latest_checkpoint(str(path), "same-thread") is None


def test_failed_switch_does_not_poison_verified_owner_recovery(tmp_path):
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
    assert result["continue"] is True
    specific = result["hookSpecificOutput"]
    assert specific["modelProvider"] == "openai"
    assert "stripProviderState" not in specific
    assert store.latest("same-thread")["strip_provider_state"] == 0


def test_incompatible_explicit_route_does_not_replace_affinity(tmp_path):
    config, store, path, _ = setup_route(tmp_path)
    before = store.latest("same-thread")["id"]
    result = invoke(
        config, store, "@azure @max continue", transcript_path=path, account_id="test-account"
    )
    assert result["continue"] is False
    assert "@gpt" in result["stopReason"]
    assert store.latest("same-thread")["id"] == before
    assert "hookSpecificOutput" not in result


def test_automatic_route_stays_with_checkpoint_owner(tmp_path):
    config, store, path, selection = setup_route(tmp_path)
    config.routing.backend_by_tier = dict.fromkeys(["fast", "normal", "smart", "max"], "azure")
    with patch("agentroute.hook.select_turn_profile", return_value=selection):
        result = invoke(
            config,
            store,
            "@auto explain the answer",
            transcript_path=path,
            account_id="test-account",
        )
    assert result["continue"] is True
    assert result["hookSpecificOutput"]["modelProvider"] == "openai"
    assert store.latest("same-thread")["sticky_backend"] is None


@pytest.mark.parametrize("current_account", [None, "different-account"])
def test_unknown_or_changed_account_cannot_clear_checkpoint_guard(tmp_path, current_account):
    config, store, path, selection = setup_route(tmp_path)
    with patch("agentroute.hook.select_turn_profile", return_value=selection):
        result = invoke(
            config, store, "@gpt @max continue", transcript_path=path, account_id=current_account
        )
    assert result["continue"] is False
    assert "matching ownership" in result["stopReason"]


def test_unknown_provider_cannot_choose_a_substitute(tmp_path):
    config, store, path, _ = setup_route(tmp_path)
    write_checkpoint(path, None)
    result = invoke(
        config, store, "@gpt @max continue", transcript_path=path, account_id="test-account"
    )
    assert result["continue"] is False


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
    assert latest_checkpoint(str(path), "same-thread").provider == "openai"


@pytest.mark.parametrize("outcome,application", [("failed", "applied"), ("pending", "pending")])
def test_intervening_account_attempt_invalidates_old_owner_evidence(tmp_path, outcome, application):
    config, store, path, selection = setup_route(tmp_path)
    invoke(config, store, "@gpt @max continue", turn_id="new-account-attempt")
    with store.connection() as db:
        db.execute(
            "UPDATE routing_decisions SET created_at='2026-01-01T00:30:00+00:00', "
            "actual_model_provider='openai', route_application_state=?, "
            "turn_outcome=?, capacity_account_id=? WHERE turn_id='new-account-attempt'",
            (application, outcome, account_hash("account-b")),
        )
    assert store.checkpoint_account("same-thread", "2026-01-01T01:00:00+00:00") is None
    with patch("agentroute.hook.select_turn_profile", return_value=selection):
        result = invoke(
            config, store, "@gpt @max continue", transcript_path=path, account_id="test-account"
        )
    assert result["continue"] is False


def test_bounded_scan_cannot_claim_older_checkpoint_absent(tmp_path, monkeypatch):
    path = tmp_path / "rollout.jsonl"
    write_checkpoint(path)
    with path.open("a") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"padding": "x" * 1024}}) + "\n")
    monkeypatch.setattr("agentroute.checkpoints.MAX_SCAN_BYTES", 128)
    assert latest_checkpoint(str(path), "same-thread") == Checkpoint(None, "")


def test_account_evidence_after_checkpoint_cannot_be_changed(tmp_path):
    config, store, path, selection = setup_route(tmp_path)
    invoke(config, store, "@gpt @max continue", turn_id="later-account")
    with store.connection() as db:
        db.execute(
            "UPDATE routing_decisions SET created_at='2026-01-01T01:30:00+00:00', "
            "capacity_account_id=? WHERE turn_id='later-account'",
            (account_hash("account-b"),),
        )
    with patch("agentroute.hook.select_turn_profile", return_value=selection):
        result = invoke(
            config, store, "@gpt @max continue", transcript_path=path, account_id="test-account"
        )
    assert result["continue"] is False


def test_checkpoint_without_later_success_cannot_clear_guard(tmp_path):
    config, store, path, selection = setup_route(tmp_path)
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n")
    with patch("agentroute.hook.select_turn_profile", return_value=selection):
        result = invoke(
            config, store, "@gpt @max continue", transcript_path=path, account_id="test-account"
        )
    assert result["continue"] is False
