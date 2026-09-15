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
