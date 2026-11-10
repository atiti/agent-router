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
        assert args[args.index("env") + 1:args.index("env") + 9] == (
            "-u", "HERDR_ENV", "-u", "HERDR_PANE_ID", "-u", "HERDR_SOCKET_PATH",
            "-u", "NO_COLOR")
        assert "--outlive-parent" in args
        assert "AGENTROUTE_HERDR_MEMBER=new-worker" in args
        assert "resume brief" in args[-1]
        return {"address": "t2"}

    monkeypatch.setattr(h, "core", launch_core)
    monkeypatch.setattr(h, "own_terminal", lambda *_: {"address": "t1", "state": "live"})
    monkeypatch.setattr(h, "view", lambda *_: None)
    monkeypatch.setattr(h, "layout", lambda *_: None)
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
    monkeypatch.setattr(h, "layout", lambda *_: None)
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


def test_dispatch_skips_blocked_assigned_and_unmet_dependencies(team, monkeypatch, tmp_path):
    items = [dict(id='blocked', status='blocked'), dict(id='owned', status='active'),
             dict(id='dependent', status='ready', dependencies=['missing'])]
    for item in items:
        item.setdefault('dependencies', [])
        item.update(lane='session', assignment=None)
    monkeypatch.setattr(h, 'packet_command', lambda _, action, *args: json.dumps(
        [] if action == 'next' else {'items': items}))
    core = Mock()
    monkeypatch.setattr(h, 'core', core)
    h.dispatch_once('demo', tmp_path, 3, {'ready'})
    core.assert_not_called()


def test_dispatch_claims_before_launch_and_never_replays_uncertain_launch(team, monkeypatch,
                                                                        tmp_path):
    item = dict(id='ready', status='ready', lane='session', assignment=None, dependencies=[],
                next_action='review', revision=1, criteria=[{'id': 'acceptance', 'evidence': []}])
    actions = []

    def packet(data, action, *args, **kwargs):
        actions.append(action)
        if action == 'status':
            return json.dumps({'items': [item]})
        if action == 'next':
            assert args == ('--role', 'worker')
            return json.dumps([{'protocol_version': 1, 'packet': 'packet', **item}])
        if action == 'claim':
            return json.dumps({'revision': 2})
        if action == 'brief':
            return 'fresh claimed brief'
        return '{"revision": 3}'

    def launch(data, *args):
        assert actions[:5] == ['status', 'next', 'claim', 'brief', 'checkpoint']
        record = h.load('demo')['members']['ready']
        assert record['packet_binding']['session'].startswith('herdr-')
        assert 'fresh claimed brief' in args[-1]
        assert 'You own the canonical claim' in args[-1]
        assert 'item=ready' in args
        raise subprocess.TimeoutExpired('hc', 30)

    monkeypatch.setattr(h, 'packet_command', packet)
    monkeypatch.setattr(h, 'core', launch)
    with pytest.raises(subprocess.TimeoutExpired):
        h.dispatch_once('demo', tmp_path, 3, {'ready'})
    assert h.load('demo')['members']['ready']['launch'] == 'unknown'
    h.dispatch_once('demo', tmp_path, 3, {'ready'})
    assert actions.count('claim') == 1


def test_dispatch_success_verifies_operation_and_tiles(team, monkeypatch, tmp_path):
    item = dict(id='ready', status='ready', lane='session', assignment=None, dependencies=[],
                next_action='review', revision=1, criteria=[])
    actions = []

    def packet(data, action, *args, **kwargs):
        actions.append((action, dict(kwargs['request']) if kwargs.get('request') else None))
        if action == 'status':
            return json.dumps({'items': [item]})
        if action == 'next':
            assert args == ('--role', 'worker')
            return json.dumps([{'protocol_version': 1, 'packet': 'packet', **item}])
        if action == 'brief':
            return 'resume brief'
        return json.dumps({'revision': len(actions)})

    monkeypatch.setattr(h, 'packet_command', packet)
    monkeypatch.setattr(h, 'core', lambda *_: {'address': 't2'})
    view, layout = Mock(), Mock()
    monkeypatch.setattr(h, 'view', view)
    monkeypatch.setattr(h, 'layout', layout)
    h.dispatch_once('demo', tmp_path, 3, {'ready'})
    operations = [request for action, request in actions if action == 'operation']
    assert [o['state'] for o in operations] == ['prepared', 'verified']
    assert operations[0]['key'] == operations[1]['key']
    view.assert_called_once_with('demo', 'ready')
    layout.assert_called_once_with('demo')


def test_worker_can_report_to_anchored_supervisor_with_durable_receipt(team, monkeypatch):
    data = h.load('demo')
    data['anchor_terminal'] = 'supervisor-terminal'
    h.save('demo', data)
    monkeypatch.setattr(h.tui, 'panes', lambda _: [
        {'terminal_id': 'supervisor-terminal', 'pane_id': 'w2:p1', 'agent': 'codex'}])
    command = Mock(return_value={'submitted': True})
    monkeypatch.setattr(h.tui, 'command', command)
    h.send('demo', 'supervisor', 'report1', 'worker report')
    h.send('demo', 'supervisor', 'report1', 'worker report')
    command.assert_not_called()
    assert h.load('demo')['messages']['report1']['state'] == 'queued'


def test_only_supervisor_prompt_is_told_how_to_dispatch(team, tmp_path):
    team["members"]["supervisor"] = {"attempt": "attempt-sup", "spec": {"role": "supervisor",
                                     "cwd": str(tmp_path), "task": "coordinate"},
                                     "agent": "codex", "launch": "started"}
    h.save("demo", team)
    supervisor = h.member_prompt(team, "supervisor", "supervisor", "coordinate")
    worker = h.member_prompt(team, "worker", "worker", "task")
    assert "team dispatch demo --dir" in supervisor
    assert "Operator and subagent items are never dispatched" in supervisor
    assert "select suitable ready items yourself" in supervisor
    assert "Before any client communication, ask Attila" in supervisor
    assert "Before any client communication, ask Attila" in worker
    assert "team dispatch" not in worker


def test_dispatch_requires_nonempty_allowlist(team, tmp_path):
    with pytest.raises(typer.BadParameter, match="allow-item"):
        h.dispatch_once("demo", tmp_path, 3, set())


def test_dispatch_ignores_ready_items_outside_allowlist(team, monkeypatch, tmp_path):
    item = {"id": "not-selected", "status": "ready", "assignment": None,
            "lane": "session", "dependencies": []}
    monkeypatch.setattr(h, "packet_command", lambda _, action, *args: json.dumps(
        [] if action == "next" else {"items": [item]}))
    core = Mock()
    monkeypatch.setattr(h, "core", core)
    h.dispatch_once("demo", tmp_path, 3, {"selected"})
    core.assert_not_called()


def test_dispatch_uses_worker_step_eligibility_for_blocked_outcome_and_preserves_hold(
        team, monkeypatch, tmp_path):
    hold = {"state": "awaiting_user", "reason": "Later production release",
            "owner": "Attila", "resume_when": "Exact deployment approval",
            "question_ref": "delivered-question"}
    item = {"id": "prepare", "status": "blocked", "lane": "session", "assignment": None,
            "dependencies": [], "next_action": "Prepare authorized source", "revision": 1,
            "criteria": [{"id": "source", "evidence": []}, {"id": "release", "evidence": []}],
            "outcome_wait": dict(hold), "next_step": {"id": "prepare-source", "state": "ready",
            "kind": "work", "action": "Prepare authorized source",
            "authority": ["source-approval"]}}
    calls = []

    def packet(data, action, *args, **kwargs):
        calls.append((action, kwargs.get("request")))
        if action == "status":
            return json.dumps({"items": [item]})
        if action == "next":
            assert args == ("--role", "worker")
            return json.dumps([{"protocol_version": 1, "packet": "packet", **item}])
        request = kwargs.get("request") or {}
        if action == "claim":
            assert request == {"expected_revision": 1, "session": request["session"]}
            assert item["outcome_wait"] == hold
            item.update(revision=2, assignment={"session": request["session"]})
            return json.dumps(item)
        if action == "brief":
            return "Execute authorized source only; later deployment remains awaiting_user."
        item["revision"] += 1
        return json.dumps(item)

    monkeypatch.setattr(h, "packet_command", packet)
    launch = Mock(return_value={"address": "t-source"})
    monkeypatch.setattr(h, "core", launch)
    monkeypatch.setattr(h, "view", lambda *_: None)
    monkeypatch.setattr(h, "layout", lambda *_: None)
    h.dispatch_once("demo", tmp_path, 3, {"prepare"})
    h.dispatch_once("demo", tmp_path, 3, {"prepare"})
    launch.assert_called_once()
    assert sum(action == "claim" for action, _ in calls) == 1
    assert item["outcome_wait"] == hold and item["status"] == "blocked"
    assert h.load("demo")["members"]["prepare"]["packet_binding"]["item"] == "prepare"
    checkpoints = [request for action, request in calls if action == "checkpoint"]
    assert checkpoints[0]["goal_review"]["remaining_criteria"] == ["source", "release"]


def test_dispatch_does_not_launch_coordinator_waits_or_unknown_reconciliation(team, monkeypatch,
                                                                            tmp_path):
    common = {"status": "blocked", "lane": "session", "assignment": None, "dependencies": [],
              "revision": 1, "criteria": [], "next_action": "Inspect"}
    items = [{**common, "id": "coordinator", "next_step": {"state": "coordination_required"}},
             {**common, "id": "wait", "next_step": {"state": "waiting_external"}},
             {**common, "id": "unknown", "next_step": {"state": "ready",
                 "kind": "reconcile_operation", "operation_key": "unknown-op"}}]
    calls = []

    def packet(data, action, *args, **kwargs):
        calls.append(action)
        if action == "status":
            return json.dumps({"items": items})
        assert action == "next" and args == ("--role", "worker")
        return json.dumps([{"protocol_version": 1, "packet": "packet", **items[-1]},
                           {"protocol_version": 1, "packet": "other-packet", **items[0]}])

    monkeypatch.setattr(h, "packet_command", packet)
    launch = Mock()
    monkeypatch.setattr(h, "core", launch)
    h.dispatch_once("demo", tmp_path, 3, {"coordinator", "wait", "unknown"})
    launch.assert_not_called()
    assert calls == ["status", "next"]
