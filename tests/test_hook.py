import io
import json

from agentroute.audit import AuditStore
from agentroute.config import default_config
from agentroute.hook import codex_user_prompt_submit


def invoke(config, store, prompt, model="gpt-5.6-terra"):
    source = io.StringIO(
        json.dumps(
            {
                "session_id": "same-thread",
                "turn_id": "turn-1",
                "model": model,
                "prompt": prompt,
            }
        )
    )
    sink = io.StringIO()
    assert codex_user_prompt_submit(source, sink, config=config, store=store) == 0
    return json.loads(sink.getvalue())


def test_observe_mode_does_not_emit_override(tmp_path):
    config = default_config()
    output = invoke(config, AuditStore(tmp_path / "audit.db"), "@smart investigate")
    specific = output["hookSpecificOutput"]

    assert "Would select SMART" in specific["additionalContext"]
    assert "model" not in specific
    assert "reasoningEffort" not in specific


def test_enabled_mode_emits_native_override_and_keeps_session_history(tmp_path):
    config = default_config()
    config.enabled = True
    store = AuditStore(tmp_path / "audit.db")

    first = invoke(config, store, "@smart investigate")
    second = invoke(config, store, "go ahead")

    assert first["hookSpecificOutput"]["model"] == "gpt-5.6-sol"
    assert first["hookSpecificOutput"]["reasoningEffort"] == "high"
    assert first["hookSpecificOutput"]["routeMessage"] == (
        "◆ MODEL ROUTE · SMART → gpt-5.6-sol · high reasoning "
        "· confidence 100% · score -0.5"
    )
    assert second["hookSpecificOutput"]["model"] == "gpt-5.6-sol"
    assert len(store.history("same-thread")) == 2


def test_hook_fails_open_on_invalid_input(tmp_path):
    sink = io.StringIO()

    assert codex_user_prompt_submit(io.StringIO("{}"), sink) == 0
    assert json.loads(sink.getvalue())["continue"] is True
