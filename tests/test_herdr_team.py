import json
import subprocess
from unittest.mock import Mock

import pytest
import typer

from agentroute import herdr_team as h


@pytest.fixture
def team(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "state_dir", lambda: tmp_path)
    data = {"id": "demo", "namespace": "markster", "packet": "packet", "item": "item",
            "core_session": "ar1234", "tui_session": "agentroute", "brief": "resume brief",
            "members": {"worker": {"attempt": "attempt-one", "spec": {"role": "worker"},
                                    "agent": "codex", "launch": "started", "address": "t1"}},
            "messages": {}}
    with h.locked("demo"):
        h.save("demo", data)
    return data


def test_namespace_is_explicit_and_slug_cannot_escape_state(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "state_dir", lambda: tmp_path)
    with pytest.raises(typer.BadParameter):
        h.path_for("../other")
    with pytest.raises(typer.BadParameter):
        h.init("demo", "Markster", "packet", "item")
    assert not (tmp_path / "herdr-teams").exists()


def test_launch_reconciliation_requires_unique_attempt_and_team_labels(team, monkeypatch):
    core = Mock(side_effect=[{"terminals": [{"address": "t1"}]}, {"address": "t1"}])
    monkeypatch.setattr(h, "core", core)
    assert h.own_terminal(team, team["members"]["worker"]) == {"address": "t1"}
    assert core.call_args_list[0].args[1:] == (
        "ls", "--all", "--json", "--label", "attempt=attempt-one", "--label", "team=demo")
    core.side_effect = None
    core.return_value = {"terminals": [{"address": "t1"}, {"address": "t2"}]}
    with pytest.raises(typer.BadParameter, match="Multiple"):
        h.own_terminal(team, team["members"]["worker"])


def test_message_intent_is_durable_before_effect_and_duplicate_is_not_sent(team, monkeypatch):
    monkeypatch.setattr(h, "own_terminal", lambda *_: {"address": "t1", "state": "live"})

    def send_core(*args, **kwargs):
        assert h.load("demo")["messages"]["m1"]["state"] == "prepared"
        return {"started": False}

    core = Mock(side_effect=send_core)
    monkeypatch.setattr(h, "core", core)
    h.send("demo", "worker", "m1", "hello")
    h.send("demo", "worker", "m1", "hello")
    assert core.call_count == 1
    assert h.load("demo")["messages"]["m1"]["state"] == "submitted"
    with pytest.raises(typer.BadParameter, match="different content"):
        h.send("demo", "worker", "m1", "changed")


def test_unknown_send_is_preserved_and_never_automatically_retried(team, monkeypatch):
    monkeypatch.setattr(h, "own_terminal", lambda *_: {"address": "t1", "state": "live"})
    core = Mock(side_effect=subprocess.TimeoutExpired("hc send", 30))
    monkeypatch.setattr(h, "core", core)
    with pytest.raises(subprocess.TimeoutExpired):
        h.send("demo", "worker", "m1", "hello")
    assert h.load("demo")["messages"]["m1"]["state"] == "unknown"
    with pytest.raises(typer.BadParameter, match="unresolved"):
        h.send("demo", "worker", "m1", "hello")
    assert core.call_count == 1


def test_finished_worker_does_not_receive_input(team, monkeypatch):
    monkeypatch.setattr(h, "own_terminal", lambda *_: {"address": "t1", "state": "finished"})
    core = Mock()
    monkeypatch.setattr(h, "core", core)
    with pytest.raises(typer.BadParameter, match="not live"):
        h.send("demo", "worker", "m1", "hello")
    assert not h.load("demo")["messages"]
    core.assert_not_called()


def test_unknown_launch_reconciles_existing_without_spawning(team, monkeypatch, tmp_path):
    prompt = tmp_path / "task.txt"
    prompt.write_text("review")
    data = h.load("demo")
    data["members"]["worker"].update(launch="unknown", spec={
        "role": "worker", "cwd": str(tmp_path.resolve()), "task": "review", "parent": None})
    h.save("demo", data)
    monkeypatch.setattr(h, "own_terminal", lambda *_: {"address": "t1", "state": "live"})
    core = Mock()
    monkeypatch.setattr(h, "core", core)
    h.run("demo", "worker", tmp_path, prompt, "worker", None)
    assert h.load("demo")["members"]["worker"]["launch"] == "reconciled"
    core.assert_not_called()


def test_missing_launch_is_not_replayed(team, monkeypatch, tmp_path):
    prompt = tmp_path / "task.txt"
    prompt.write_text("review")
    data = h.load("demo")
    data["members"]["worker"]["spec"] = {
        "role": "worker", "cwd": str(tmp_path.resolve()), "task": "review", "parent": None}
    h.save("demo", data)
    monkeypatch.setattr(h, "own_terminal", lambda *_: None)
    with pytest.raises(typer.BadParameter, match="unresolved"):
        h.run("demo", "worker", tmp_path, prompt, "worker", None)


def test_launch_intent_precedes_effect_and_workers_keep_identity(team, monkeypatch, tmp_path):
    prompt = tmp_path / "task.txt"
    prompt.write_text("review")

    def launch_core(data, *args):
        record = h.load("demo")["members"]["new-worker"]
        assert record["launch"] == "prepared"
        assert "--no-daemon" in args
        assert "--outlive-parent" in args
        assert "AGENTROUTE_HERDR_MEMBER=new-worker" in args
        assert "resume brief" in args[-1]
        return {"address": "t2"}

    monkeypatch.setattr(h, "core", launch_core)
    monkeypatch.setattr(h, "own_terminal", lambda *_: {"address": "t1", "state": "live"})
    monkeypatch.setattr(h, "view", lambda *_: None)
    h.run("demo", "new-worker", tmp_path, prompt, "worker", "worker")
    assert h.load("demo")["members"]["new-worker"]["address"] == "t2"


def test_filters_do_not_convert_idle_status_to_packet_completion(team, monkeypatch, capsys):
    monkeypatch.setattr(h, "own_terminal", lambda *_: {
        "state": "live", "status": {"app": "codex", "state": "idle"}})
    h.status("demo", "worker", "idle")
    assert len(json.loads(capsys.readouterr().out)["members"]) == 1
    h.status("demo", "supervisor", None)
    assert not json.loads(capsys.readouterr().out)["members"]
    assert "gates" not in h.load("demo")


def test_explicit_resume_preserves_thread_without_replaying_initial_task(team, monkeypatch):
    data = h.load("demo")
    data["members"]["worker"].update(thread="11111111-1111-1111-1111-111111111111",
        spec={"role": "worker", "cwd": "/tmp", "task": "never replay me", "parent": None})
    h.save("demo", data)
    monkeypatch.setattr(h, "own_terminal", lambda *_: {"state": "finished"})

    def launch_core(data, *args):
        assert args[-2:] == ("resume", "11111111-1111-1111-1111-111111111111")
        assert "never replay me" not in args
        assert h.load("demo")["members"]["worker"]["launch"] == "prepared"
        return {"address": "t9"}

    monkeypatch.setattr(h, "core", launch_core)
    monkeypatch.setattr(h, "view", lambda *_: None)
    h.resume("demo", "worker")
    current = h.load("demo")
    assert current["members"]["worker"]["address"] == "t9"
    assert current["history"][0]["attempt"] == "attempt-one"


def test_uncertain_resume_cannot_create_another_attempt(team, monkeypatch):
    data = h.load("demo")
    data["members"]["worker"].update(
        launch="unknown", thread="11111111-1111-1111-1111-111111111111")
    h.save("demo", data)
    monkeypatch.setattr(h, "own_terminal", lambda *_: None)
    with pytest.raises(typer.BadParameter, match="unresolved"):
        h.resume("demo", "worker")
    assert h.load("demo")["members"]["worker"]["attempt"] == "attempt-one"
