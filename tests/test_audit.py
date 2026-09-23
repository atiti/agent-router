import json

from agentroute.audit import AuditStore
from agentroute.config import default_config
from agentroute.models import RouteContext, Tier
from agentroute.router import Router


def test_audit_defaults_to_prompt_hash_only(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    decision = Router(default_config()).route(
        RouteContext(session_id="session-1", latest_prompt="@smart diagnose it")
    )

    store.record(decision, Tier.NORMAL)
    row = store.latest("session-1")

    assert row is not None
    assert row["prompt"] is None
    assert len(row["prompt_hash"]) == 64
    assert store.previous_tier("session-1") is Tier.SMART
    assert row["classifier_version"] == "hybrid-v9"
    assert row["classification_source"] == "manual"
    assert row["comparison_tier"] == "normal"
    assert len(row["selection_receipt_hash"]) == 64
    receipt = json.loads(row["selection_receipt"])
    assert receipt["selected"]["model"] == "gpt-6-sol"
    assert receipt["version"] == "selection-v1"


def test_previous_route_is_scoped_per_child(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    router = Router(default_config())
    root = router.route(RouteContext(session_id="session-1", latest_prompt="@normal root"))
    child_a = router.route(
        RouteContext(
            session_id="session-1",
            route_scope="subagent",
            agent_id="/root/a",
            latest_prompt="@smart child a",
        )
    )
    child_b = router.route(
        RouteContext(
            session_id="session-1",
            route_scope="subagent",
            agent_id="/root/b",
            latest_prompt="@fast child b",
        )
    )
    store.record(root, Tier.NORMAL)
    store.record(child_a, Tier.NORMAL)
    store.record(child_b, Tier.NORMAL)

    assert store.previous_tier("session-1") is Tier.NORMAL
    assert store.previous_tier("session-1", "subagent", "/root/a") is Tier.SMART
    assert store.previous_tier("session-1", "subagent", "/root/b") is Tier.FAST
    assert store.previous_backend("session-1", "subagent", "/root/a") == "gpt"


def test_manual_override_records_signal_without_asserting_previous_quality(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    router = Router(default_config())
    automatic = router.route(
        RouteContext(session_id="session-1", latest_prompt="routine work")
    )
    store.record(automatic, Tier.NORMAL)
    manual = router.route(
        RouteContext(
            session_id="session-1",
            latest_prompt="@smart actually use the stronger model",
            previous_task_tier=automatic.tier,
        )
    )
    store.record(manual, Tier.NORMAL)

    previous = store.history("session-1", limit=2)[1]

    assert previous["outcome_label"] is None
    assert previous["next_manual_override_tier"] == "smart"
    assert previous["next_manual_override_at"] is not None


def test_manual_override_signal_is_scoped_to_the_same_agent(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    router = Router(default_config())
    root_id = store.record(
        router.route(RouteContext(session_id="s", latest_prompt="routine root work")),
        Tier.NORMAL,
    )
    child_id = store.record(
        router.route(
            RouteContext(
                session_id="s",
                route_scope="subagent",
                agent_id="/root/child",
                latest_prompt="routine child work",
            )
        ),
        Tier.NORMAL,
    )
    store.record(
        router.route(
            RouteContext(
                session_id="s",
                route_scope="subagent",
                agent_id="/root/child",
                latest_prompt="@smart continue child work",
            )
        ),
        Tier.NORMAL,
    )

    rows = {row["id"]: row for row in store.history(limit=10)}
    assert rows[root_id]["next_manual_override_tier"] is None
    assert rows[child_id]["next_manual_override_tier"] == "smart"


def test_audit_schema_has_calibration_columns(tmp_path):
    path = tmp_path / "audit.db"
    store = AuditStore(path)
    with store.connection() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(routing_decisions)")
        }

    assert "classifier_version" in columns
    assert "outcome_label" in columns
    assert "agent_requested_tier" in columns
    assert "agent_request_reason_hash" in columns
    assert "classification_source" in columns
    assert "classifier_confidence" in columns
    assert "classifier_reasoning_effort" in columns
    assert "reasoning_effort_source" in columns
    assert "classifier_reason_hash" in columns
    assert "selection_receipt" in columns
    assert "selection_receipt_hash" in columns
    assert "previous_context_sent" in columns
    assert "resolved_task_inherited" in columns
    assert "classifier_latency_ms" in columns
    assert "classifier_request_hash" in columns
    assert "classifier_usage" in columns
    assert "classifier_status" in columns
    assert "classifier_error_type" in columns
    assert "turn_id" in columns
    assert "answer_input_tokens" in columns
    assert "usage_recorded_at" in columns
    assert "reported_answer_model" in columns
    assert "answer_model_mismatch" in columns
    assert "turn_completed_at" in columns
    assert "turn_duration_ms" in columns
    assert "turn_outcome" in columns
    assert "completion_source" in columns
    assert "usage_status" in columns
    assert "sticky_backend" in columns
    assert "strip_provider_state" in columns
    assert "next_manual_override_tier" in columns
    assert "next_manual_override_at" in columns


def test_completion_normalizes_legacy_stop_model_and_records_duration(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    decision = Router(default_config()).route(
        RouteContext(session_id="session-1", turn_id="turn-1", latest_prompt="show status")
    )
    decision.backend = "deepseek"
    decision.model = "deepseek-flash"
    store.record(decision, Tier.NORMAL)

    assert store.record_completion(
        "session-1",
        "turn-1",
        "gpt-5.6-terra",
        {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
    )
    row = store.latest("session-1")

    assert row["answer_model"] == "deepseek-flash"
    assert row["reported_answer_model"] == "gpt-5.6-terra"
    assert row["answer_model_mismatch"] == 1
    assert row["turn_completed_at"] is not None
    assert row["turn_duration_ms"] >= 0


def test_completion_is_recorded_without_token_usage(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    decision = Router(default_config()).route(
        RouteContext(session_id="session-1", turn_id="turn-1", latest_prompt="show status")
    )
    store.record(decision, Tier.NORMAL)

    assert store.record_completion("session-1", "turn-1", decision.model)
    row = store.latest("session-1")

    assert row["turn_completed_at"] is not None
    assert row["turn_duration_ms"] >= 0
    assert row["usage_recorded_at"] is None
    assert row["turn_outcome"] == "completed"
    assert row["usage_status"] == "missing"


def test_existing_stale_stop_receipt_is_normalized_on_open(tmp_path):
    path = tmp_path / "audit.db"
    store = AuditStore(path)
    decision = Router(default_config()).route(
        RouteContext(session_id="session-1", turn_id="turn-1", latest_prompt="show status")
    )
    decision.backend = "deepseek"
    decision.model = "deepseek-flash"
    identifier = store.record(decision, Tier.NORMAL)
    with store.connection() as connection:
        connection.execute(
            "UPDATE routing_decisions SET answer_model = ?, answer_total_tokens = ?, "
            "usage_recorded_at = created_at WHERE id = ?",
            ("gpt-5.6-terra", 110, identifier),
        )

    reopened = AuditStore(path)
    row = reopened.latest("session-1")

    assert row["answer_model"] == "deepseek-flash"
    assert row["reported_answer_model"] == "gpt-5.6-terra"
    assert row["answer_model_mismatch"] == 1
    assert row["turn_completed_at"] == row["usage_recorded_at"]
    assert row["turn_duration_ms"] == 0
