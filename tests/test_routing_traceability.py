import json

from typer.testing import CliRunner

from agentroute.audit import AuditStore
from agentroute.cli import app
from agentroute.config import default_config
from agentroute.models import RouteContext, Tier
from agentroute.router import Router

runner = CliRunner()


def test_decision_details_and_report_include_provider_evidence(tmp_path, monkeypatch):
    store = AuditStore(tmp_path / "audit.db")
    decision = Router(default_config()).route(
        RouteContext(session_id="test-session", latest_prompt="hello")
    )
    decision_id = store.record(decision, Tier.NORMAL)
    selection = {
        "provider_selection": {
            "mode": "adaptive",
            "selection_pool": "subscription",
            "selected_backend": "gpt",
            "recommended_backend": "gpt",
            "locked_by": None,
            "candidates": [
                {
                    "backend": "gpt",
                    "billing_class": "subscription",
                    "eligible": True,
                    "score": 1.5,
                    "used_percent": 0,
                    "capacity_status": "healthy",
                    "exclusions": [],
                },
                {
                    "backend": "deepseek",
                    "billing_class": "other",
                    "eligible": False,
                    "exclusions": ["budget_reached"],
                },
            ],
        }
    }
    with store.connection() as c:
        c.execute(
            "UPDATE routing_decisions SET selection_receipt = ? WHERE id = ?",
            (json.dumps(selection), decision_id),
        )
    monkeypatch.setattr("agentroute.cli.AuditStore", lambda: store)
    detail = runner.invoke(app, ["why", "--decision-id", str(decision_id)])
    assert detail.exit_code == 0, detail.output
    assert "pool=subscription" in detail.output
    assert "budget_reached" in detail.output
    assert "used=0%" in detail.output
    report = runner.invoke(app, ["audit-report"])
    assert report.exit_code == 0, report.output
    assert "subscription=1" in report.output
    assert "budget_reached=1" in report.output
    assert "unlabeled" in report.output
    assert runner.invoke(app, ["why", "--decision-id", "999999"]).exit_code == 1


def test_legacy_malformed_receipt_does_not_break_inspection(tmp_path, monkeypatch):
    store = AuditStore(tmp_path / "audit.db")
    decision = Router(default_config()).route(
        RouteContext(session_id="legacy", latest_prompt="hello")
    )
    decision_id = store.record(decision, Tier.NORMAL)
    with store.connection() as c:
        c.execute(
            "UPDATE routing_decisions SET selection_receipt = ? WHERE id = ?", ("{", decision_id)
        )
    monkeypatch.setattr("agentroute.cli.AuditStore", lambda: store)
    assert runner.invoke(app, ["why", "--decision-id", str(decision_id)]).exit_code == 0
    assert runner.invoke(app, ["audit-report"]).exit_code == 0
