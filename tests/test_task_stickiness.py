from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from agentroute.audit import AuditStore
from agentroute.config import default_config
from agentroute.models import ReasonCode, RouteContext, Tier
from agentroute.router import Router

# Pytest's importlib mode does not put the repository root on sys.path, so the
# sibling helpers cannot be imported as a package. Loading the file registers
# setup_policy as a fixture in this module.
_spec = importlib.util.spec_from_file_location(
    "task_stickiness_selection_helpers",
    Path(__file__).with_name("test_provider_selection.py"),
)
_selection = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_selection)
selection_context = _selection.context
select_provider = _selection.select_provider
setup_policy = _selection.setup_policy

LARGE = {"input_tokens": 220_000, "cached_input_tokens": 210_000, "output_tokens": 900}
MECHANICAL = "Rename the label and fix the typo"


def decide(prompt: str, **kwargs):
    context = RouteContext(session_id="sticky", latest_prompt=prompt, **kwargs)
    return Router(default_config()).route(context)


def test_one_lower_proposal_is_held_once_the_context_is_large():
    decision = decide(MECHANICAL, current_tier=Tier.SMART, previous_response_usage=LARGE)
    assert decision.tier is Tier.SMART
    assert ReasonCode.TASK_STICKINESS in decision.reason_codes
    state = decision.selection_receipt["task_stickiness"]
    assert state["held"] and state["streak"] == 1 and state["proposed"] == "fast"


def test_a_repeated_lower_proposal_is_applied():
    decision = decide(
        MECHANICAL, current_tier=Tier.SMART, previous_response_usage=LARGE, downgrade_streak=1
    )
    assert decision.tier is Tier.FAST
    assert decision.selection_receipt["task_stickiness"]["held"] is False


def test_small_context_and_upgrades_and_explicit_tags_route_freely():
    small = {"input_tokens": 9_000, "cached_input_tokens": 0, "output_tokens": 100}
    assert (
        decide(MECHANICAL, current_tier=Tier.SMART, previous_response_usage=small).tier is Tier.FAST
    )
    upgrade = decide(
        "Design the database migration and security review for the auth service",
        current_tier=Tier.FAST,
        previous_response_usage=LARGE,
    )
    assert upgrade.tier > Tier.FAST and "task_stickiness" not in upgrade.selection_receipt
    manual = decide("@fast " + MECHANICAL, current_tier=Tier.SMART, previous_response_usage=LARGE)
    assert manual.tier is Tier.FAST


def test_streak_is_read_from_the_previous_decision_and_resets(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    assert store.downgrade_streak("sticky") == 0
    held = decide(MECHANICAL, current_tier=Tier.SMART, previous_response_usage=LARGE)
    store.record(held, Tier.SMART)
    assert store.downgrade_streak("sticky") == 1
    applied = decide(
        MECHANICAL, current_tier=Tier.SMART, previous_response_usage=LARGE, downgrade_streak=1
    )
    store.record(applied, Tier.SMART)
    assert store.downgrade_streak("sticky") == 0


def test_provider_switch_penalty_grows_with_measured_context(setup_policy):  # noqa: F811
    config, _ = setup_policy
    grok = config.backends["grok"]
    current = {
        "current_model": grok.tiers["smart"].model,
        "current_model_provider": grok.codex_provider,
    }

    def choose(tokens: int):
        usage = {"input_tokens": tokens}
        return select_provider(
            config,
            selection_context(previous_response_usage=usage, **current),
            Tier.SMART,
            "architecture",
            "gpt",
        )

    # With little context the task preference for Claude wins; with a long conversation the
    # cost of re-reading it keeps the active provider.
    backend, _ = choose(2_000)
    assert backend == "claude"
    backend, receipt = choose(300_000)
    assert backend == "grok"
    claude = next(row for row in receipt["candidates"] if row["backend"] == "claude")
    assert claude["switch_penalty"] > 5


def test_exhausted_active_provider_still_fails_over_despite_large_context(
    setup_policy,  # noqa: F811
):
    config, states = setup_policy
    states["gpt"] = type(states["gpt"])("gpt", "exhausted", "limit reached", used_percent=100)
    backend, _ = select_provider(
        config,
        selection_context(
            previous_response_usage={"input_tokens": 300_000},
            current_model="gpt-6.1-sol",
            current_model_provider="openai",
        ),
        Tier.SMART,
        "implementation",
        "gpt",
    )
    assert backend != "gpt"


def test_task_summary_and_sample_support_later_labelling(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    decision = decide("Explain   why the\nnightly import failed and propose a fix " + "x" * 300)
    decision_id = store.record(decision, Tier.NORMAL)
    store.set_task_summary(decision_id, "Explain   why the\nnightly import failed " + "x" * 300)
    with store.connection() as connection:
        connection.execute(
            "UPDATE routing_decisions SET turn_outcome='completed', "
            "route_application_state='applied', classification_source='cloud_jev' WHERE id = ?",
            (decision_id,),
        )
    rows = store.sample(count=5)
    assert [row["id"] for row in rows] == [decision_id]
    assert rows[0]["task_summary"].startswith("Explain why the nightly import failed")
    assert len(rows[0]["task_summary"]) == 160
    store.label(decision_id, "correct")
    assert store.sample(count=5) == []


def test_read_only_store_can_still_be_queried(tmp_path):
    path = tmp_path / "audit.db"
    store = AuditStore(path)
    store.record(decide("Summarize the changelog"), Tier.NORMAL)
    path.chmod(0o444)
    tmp_path.chmod(0o555)
    try:
        assert len(AuditStore(path).history(limit=5)) == 1
    finally:
        tmp_path.chmod(0o755)
        path.chmod(0o644)
    assert json.loads(AuditStore(path).history(limit=1)[0]["selection_receipt"])
