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
    assert row["classifier_version"] == "heuristic-v3"
    assert row["comparison_tier"] == "normal"


def test_manual_override_labels_previous_automatic_decision(tmp_path):
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

    assert previous["outcome_label"] == "overridden"


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
