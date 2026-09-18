from datetime import datetime, timedelta, timezone

import pytest

from agentroute.analytics import usage_analytics
from agentroute.audit import AuditStore
from agentroute.config import default_config
from agentroute.models import RouteContext, Tier
from agentroute.router import Router


def _record_turn(store, config, *, session, turn, backend, answer_model, created_at, usage):
    decision = Router(config).route(
        RouteContext(session_id=session, turn_id=turn, latest_prompt="show current status")
    )
    decision.backend = backend
    decision.classifier_usage = {"prompt_tokens": 100, "completion_tokens": 10}
    decision.classifier_latency_ms = 250
    decision.selection_receipt["classifier"]["model"] = "gpt-5.6-luna"
    identifier = store.record(decision, Tier.NORMAL)
    with store.connection() as connection:
        connection.execute(
            "UPDATE routing_decisions SET created_at = ? WHERE id = ?",
            (created_at.isoformat(), identifier),
        )
    store.record_usage(session, turn, answer_model, usage)


def test_usage_analytics_groups_models_and_days(tmp_path):
    config = default_config()
    store = AuditStore(tmp_path / "audit.db")
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    _record_turn(
        store,
        config,
        session="s",
        turn="one",
        backend="gpt",
        answer_model="gpt-5.6-luna",
        created_at=now,
        usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    )
    _record_turn(
        store,
        config,
        session="s",
        turn="two",
        backend="azure",
        answer_model="gpt-5.6-sol",
        created_at=now - timedelta(days=1),
        usage={"input_tokens": 200, "output_tokens": 40, "total_tokens": 240},
    )

    report = usage_analytics(store.rows_since(), config.pricing, "gpt-6-astra", bucket="day")

    assert report.rows == 2
    assert report.overall.measured_turns == 2
    assert {(item.backend, item.model, item.total_tokens) for item in report.by_model} == {
        ("gpt", "gpt-5.6-luna", 120),
        ("azure", "gpt-5.6-sol", 240),
    }
    assert len(report.over_time) == 2
    assert report.classifiers[0].model == "gpt-5.6-luna"
    assert report.classifiers[0].calls == 2
    assert report.classifiers[0].total_tokens == 220


def test_usage_analytics_rejects_unknown_bucket(tmp_path):
    with pytest.raises(ValueError, match="day, week, or month"):
        usage_analytics([], default_config().pricing, "gpt-6-astra", bucket="quarter")


def test_rows_since_filters_session_and_time(tmp_path):
    config = default_config()
    store = AuditStore(tmp_path / "audit.db")
    now = datetime.now(timezone.utc)
    _record_turn(
        store,
        config,
        session="included",
        turn="one",
        backend="gpt",
        answer_model="gpt-5.6-luna",
        created_at=now,
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    _record_turn(
        store,
        config,
        session="old",
        turn="two",
        backend="gpt",
        answer_model="gpt-5.6-luna",
        created_at=now - timedelta(days=3),
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )

    rows = store.rows_since(now - timedelta(days=1), session_id="included")

    assert len(rows) == 1
    assert rows[0]["session_id"] == "included"
