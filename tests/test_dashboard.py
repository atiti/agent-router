from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from agentroute import dashboard, dashboard_server
from agentroute.cli import app

ROOT = "01a20744-50ee-71a2-a6a3-3e26905fc41b"
WORKER = "01a20d0f-f3a5-7bb0-93a1-c3408f558f88"
REVIEW = "01a20e57-15de-7c52-b8c1-c3d328a4db76"
WAITER = "01a1bf89-2c42-7852-8687-e490794cb825"
UNUSED = "01a1bf61-c05f-7f71-abe2-98de6d38fd2e"
NOW = 1_795_592_000.0


def event(kind: str, at: float, **payload) -> str:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))
    return json.dumps({"timestamp": stamp, "type": "event_msg",
                       "payload": {"type": kind, **payload}}) + "\n"


def item(at: float, **fields) -> str:
    return event("item_completed", at, item=fields)


def shell(at: float, script: str, exit_code: int = 0) -> str:
    return item(at, type="CommandExecution", command=["/bin/zsh", "-lc", script],
                cwd=str(Path.home() / "build/agent-router"), exit_code=exit_code,
                duration={"secs": 2, "nanos": 0})


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A small copy of the state the dashboard reads: threads, goals, routes, waits, packets."""
    codex, home = Path(os.environ["CODEX_HOME"]), Path(os.environ["AGENTROUTE_HOME"])
    codex.mkdir(parents=True)
    (home / "state" / "waits").mkdir(parents=True)
    monkeypatch.setenv("WORK_PACKETS_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setattr(dashboard, "herdr_panes", lambda: {})
    monkeypatch.setattr(dashboard, "owner_states", lambda: None)

    rollouts = {name: tmp_path / f"{name}.jsonl" for name in ("root", "worker", "waiter")}
    rollouts["root"].write_text(
        event("task_started", NOW - 120, turn_id="t2", started_at=NOW - 120)
        + shell(NOW - 100, "cd ~/build/agent-router && GH_TOKEN=ghp_abcdefgh12345678 "
                           "gh pr view 204 --repo atiti/agent-router")
        + shell(NOW - 90, "uv run pytest tests/test_dashboard.py", exit_code=1)
        + item(NOW - 80, type="FileChange", status="completed",
               changes={str(Path.home() / "build/agent-router/src/x.py"): {}})
        + item(NOW - 70, type="McpToolCall", server="contactdb", tool="search",
               status="completed")
        + item(NOW - 60, type="AgentMessage", id="m1", phase="commentary",
               content=[{"text": "Wiring the CLI next."}])
        + event("token_count", NOW - 50,
                info={"total_token_usage": {"total_tokens": 900},
                      "last_token_usage": {"input_tokens": 300},
                      "model_context_window": 1000})
    )
    rollouts["worker"].write_text(
        event("task_started", NOW - 4000, turn_id="w1", started_at=NOW - 4000)
        + shell(NOW - 3990, "marksterctl exec --host app1 --cmd uptime")
        + event("task_complete", NOW - 3900, turn_id="w1", duration_ms=100_000)
    )
    rollouts["waiter"].write_text(
        event("task_started", NOW - 9000, turn_id="x1")
        + event("task_complete", NOW - 8000, turn_id="x1")
    )

    with sqlite3.connect(codex / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, created_at INT, "
                   "updated_at INT, source TEXT, cwd TEXT, title TEXT, tokens_used INT, "
                   "archived INT, git_branch TEXT, model TEXT, reasoning_effort TEXT, "
                   "agent_nickname TEXT, agent_role TEXT, agent_path TEXT, name TEXT)")
        db.execute("CREATE TABLE task_work (thread_id TEXT, title TEXT, summary TEXT, "
                   "status TEXT, updated_at INT)")
        spawn = json.dumps({"subagent": {"thread_spawn": {
            "parent_thread_id": ROOT, "agent_path": "/root/state_sources",
            "agent_nickname": "Laplace"}}})
        home_dir = str(Path.home())
        db.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,0,?,?,?,NULL,NULL,NULL,NULL)", [
            (ROOT, str(rollouts["root"]), NOW - 9000, NOW - 50, "cli",
             f"{home_dir}/build/agent-router", "Dashboard", 5000, "main", "gpt-6.2-sol", "high"),
            (WORKER, str(rollouts["worker"]), NOW - 4000, NOW - 3900, spawn,
             f"{home_dir}/build/markster-ops", "", 700, None, "gpt-6.1-terra", "medium"),
            (REVIEW, None, NOW - 500, NOW - 400, json.dumps({"subagent": "review"}),
             "/tmp", "review", 40, None, None, None),
            (WAITER, str(rollouts["waiter"]), NOW - 9000, NOW - 8000, "cli",
             "/tmp", "Release watch", 300, None, None, None),
            (UNUSED, None, NOW - 600, NOW - 600, "cli", "/private/tmp", "", 0,
             None, None, None),
        ])
        db.execute("INSERT INTO task_work VALUES (?,?,?,?,?)",
                   (ROOT, "AgentRoute live agent dashboard", "Server drafted", "working", NOW))
    with sqlite3.connect(codex / "goals_1.sqlite") as db:
        db.execute("CREATE TABLE thread_goals (thread_id TEXT, goal_id TEXT, objective TEXT, "
                   "status TEXT, tokens_used INT, token_budget INT, time_used_seconds INT, "
                   "updated_at_ms INT)")
        db.execute("CREATE TABLE thread_goal_completion_reviews (thread_id TEXT, goal_id TEXT, "
                   "decision TEXT, reason TEXT, attempts INT)")
        db.execute("INSERT INTO thread_goals VALUES (?,?,?,?,?,?,?,?)",
                   (ROOT, "g1", "do it end to end", "active", 100, None, 60, NOW * 1000))
    with sqlite3.connect(home / "audit.db") as db:
        db.execute("CREATE TABLE routing_decisions (id INTEGER PRIMARY KEY, created_at TEXT, "
                   "session_id TEXT, agent_id TEXT, turn_id TEXT, selected_tier TEXT, "
                   "model TEXT, actual_model TEXT, reasoning_effort TEXT, "
                   "actual_reasoning_effort TEXT, backend TEXT, actual_backend TEXT, "
                   "turn_outcome TEXT, answer_total_tokens INT, answer_input_tokens INT, "
                   "answer_cached_input_tokens INT, answer_output_tokens INT, "
                   "turn_duration_ms INT, classifier_task_type TEXT, task_summary TEXT, "
                   "reason_codes TEXT)")

        def route(at, session, agent, turn, model, tokens, outcome="completed"):
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(at))
            db.execute(
                "INSERT INTO routing_decisions (created_at, session_id, agent_id, turn_id, "
                "selected_tier, model, reasoning_effort, backend, turn_outcome, "
                "answer_total_tokens, answer_cached_input_tokens, reason_codes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (stamp, session, agent, turn, "smart", model, "high", "gpt", outcome,
                 tokens, tokens and tokens // 2, '["INHERITANCE"]'))

        # One turn routed three times: a steering row with no usage, then two running totals.
        route(NOW - 3000, ROOT, None, "t1", "gpt-6.2-sol", None, "pending")
        route(NOW - 2900, ROOT, None, "t1", "gpt-6.2-sol", 1000)
        route(NOW - 2800, ROOT, None, "t1", "gpt-6.2-sol", 1600)
        route(NOW - 120, ROOT, None, "t2", "claude-fable-5-2", None, "pending")
        route(NOW - 4000, ROOT, WORKER, "w1", "gpt-6.1-terra", 700)
    (home / "state" / "waits" / "waits.json").write_text(json.dumps({"version": 1, "waits": [
        {"id": "w-11111111", "thread_id": WAITER, "kind": "command", "spec": {},
         "state": "pending", "note": "Poll the download; --token abc123 must not show",
         "created_at": NOW - 7000, "deadline_at": NOW + 3600},
        {"id": "w-22222222", "thread_id": ROOT, "kind": "pr", "state": "delivered",
         "spec": {"repo": "atiti/agent-router", "number": 204, "until": "checks"},
         "note": "", "created_at": NOW - 5000, "deadline_at": NOW - 1000,
         "outcome": {"reason": "condition"}},
    ]}))
    packets = tmp_path / "workspace" / "tasks" / "packets" / "private"
    packets.mkdir(parents=True)
    (packets / "agentroute-dashboard.json").write_text(json.dumps({
        "id": "agentroute-dashboard", "namespace": "private", "objective": "Ship the dashboard",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 30)),
        "items": [
            {"id": "build", "objective": "Build it", "status": "active", "priority": "P1",
             "next_action": "Write tests", "assignment": {"session": ROOT},
             "criteria": [{"evidence": "ran"}, {}], "gates": {"ci": {"status": "pending"}}},
            {"id": "plan", "status": "done"},
        ]}))
    monkeypatch.setattr(time, "time", lambda: NOW)
    return dashboard.Collector(["Ana"])


@pytest.mark.parametrize(("text", "hidden", "kept"), [
    ("curl -H 'Authorization: Bearer abc.def-123456' https://x.example", "abc.def", "x.example"),
    ("GH_TOKEN=ghp_abcdefgh12345678 gh pr view 12", "ghp_", "gh pr view 12"),
    ("tool --api-key sk-live-123456789 --verbose", "sk-live", "--verbose"),
    ('{"api_key": "hunter2hunter2", "name": "x"}', "hunter2", '"name": "x"'),
    ("git push https://ghs_abcdefgh12345678@github.com/a/b", "ghs_", "github.com/a/b"),
    ("curl -u x -H 'authorization: basic dXNlcjpwYXNz'", "dXNlcjpwYXNz", "curl -u x"),
])
def test_redact_masks_credentials_and_keeps_the_rest(text, hidden, kept):
    shown = dashboard.redact(text)
    assert hidden not in shown
    assert kept in shown


def test_redact_leaves_prose_readable_and_bounds_length():
    assert dashboard.redact("Token usage dashboard for agents") == \
        "Token usage dashboard for agents"
    assert dashboard.redact("a\n  b") == "a b"
    assert len(dashboard.redact("x" * 500, 100)) == 100


@pytest.mark.parametrize(("script", "name"), [
    ("cd repo && GH_TOKEN=x gh pr view 1", "gh"),
    ("set -e\nexport A=1\n/usr/bin/env python3 run.py", "python3"),
    ("sudo -n -H systemctl restart x", "systemctl"),
    ("for f in *; do echo $f; done", None),
    ("cd somewhere", None),
])
def test_program_name_skips_setup_and_wrappers(script, name):
    assert dashboard.program_name(script) == name


def test_command_targets_finds_what_a_command_reaches():
    targets = dashboard.command_targets(
        "gh pr view 204 --repo atiti/agent-router && marksterctl deploy --service core "
        "--host app1 && curl https://api.example.com/v1 https://github.com/atiti/agent-router/")
    assert ("github", "atiti/agent-router") in targets
    assert ("pr", "atiti/agent-router#204") in targets
    assert ("host", "app1") in targets
    assert ("service", "core") in targets
    assert ("web", "api.example.com") in targets
    assert not any(label == "github.com" for _, label in targets)


def test_thread_kind_reads_the_codex_source_column():
    assert dashboard.thread_kind("cli") == ("root", {})
    assert dashboard.thread_kind("exec") == ("exec", {})
    assert dashboard.thread_kind('{"subagent": "review"}') == ("review", {})
    assert dashboard.thread_kind('{"subagent": {"other": "guardian"}}') == ("guardian", {})
    kind, spawn = dashboard.thread_kind(
        '{"subagent": {"thread_spawn": {"parent_thread_id": "p", "agent_nickname": "Bohr"}}}')
    assert (kind, spawn["parent_thread_id"], spawn["agent_nickname"]) == ("worker", "p", "Bohr")
    assert dashboard.thread_kind("{not json") == ("root", {})


def agent(**overrides):
    base = {"kind": "root", "activity": {"turn": None, "last_turn": None}, "waits": [],
            "pane": None, "goal": None, "task": None, "active_at": NOW}
    return {**base, **overrides}


@pytest.mark.parametrize(("fields", "expected"), [
    ({"pane": {"status": "blocked"}, "activity": {"turn": {"tools": 1}}}, "needs_input"),
    ({"activity": {"turn": {"tools": 1}}, "waits": [{"description": "w"}]}, "working"),
    ({"activity": {"turn": {"tools": 1}}, "active_at": NOW - 3600}, "stalled"),
    ({"waits": [{"description": "w-1 pr"}], "goal": {"status": "blocked"}}, "waiting"),
    ({"goal": {"status": "usage_limited"}}, "blocked"),
    ({"task": {"status": "waiting", "summary": "on CI"}}, "waiting"),
    ({"task": {"status": "completed", "summary": "merged"}}, "done"),
    ({"kind": "exec", "activity": {"turn": None, "last_turn": {"outcome": "completed"}}},
     "done"),
    ({"goal": {"status": "paused"}}, "idle"),
    ({}, "idle"),
])
def test_agent_state_orders_by_who_must_act(fields, expected):
    assert dashboard.agent_state(agent(**fields), NOW)[0] == expected


def test_rollout_tail_reads_only_new_complete_lines(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(event("task_started", NOW, turn_id="a") + shell(NOW + 1, "git status"))
    tail = dashboard.RolloutTail(path)
    tail.refresh()
    assert tail.turn["tools"] == 1 and tail.tools == {"shell": 1}

    half = shell(NOW + 2, "ls")
    with path.open("a") as stream:
        stream.write(half[:40])
    tail.refresh()
    assert tail.tools == {"shell": 1}  # A line still being written is left for the next read.
    with path.open("a") as stream:
        stream.write(half[40:] + event("task_complete", NOW + 3, turn_id="a"))
    tail.refresh()
    assert tail.tools == {"shell": 2}
    assert tail.turn is None and tail.last_turn["outcome"] == "completed"
    assert tail.last_turn["tools"] == 2

    path.write_text(event("task_started", NOW + 9, turn_id="b"))  # Rewritten, so start over.
    tail.refresh()
    assert tail.tools == {} and tail.turn["id"] == "b"


def test_rollout_tail_sees_a_turn_that_started_before_the_part_it_read(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "INITIAL_TAIL_BYTES", 600)
    path = tmp_path / "long.jsonl"
    path.write_text(event("task_started", NOW, turn_id="a")
                    + "".join(shell(NOW + n, f"echo {n}") for n in range(1, 30)))
    tail = dashboard.RolloutTail(path)
    tail.refresh()
    view = tail.view()
    assert view["partial"] and view["turn"] is not None
    assert view["turn"]["started_at"] is None
    assert 0 < view["tools"]["shell"] < 29

    finished = tmp_path / "finished.jsonl"
    finished.write_text("".join(shell(NOW + n, f"echo {n}") for n in range(1, 30))
                        + event("task_complete", NOW + 40, turn_id="a"))
    tail = dashboard.RolloutTail(finished)
    tail.refresh()
    assert tail.turn is None and tail.last_turn["outcome"] == "completed"


def test_collapse_turns_counts_a_turn_once():
    rows = [
        {"session_id": "s", "turn_id": "t", "answer_total_tokens": None, "model": "m",
         "created_at": "2026-11-24T10:00:00+00:00", "turn_outcome": "pending"},
        {"session_id": "s", "turn_id": "t", "answer_total_tokens": 1000, "model": "m",
         "created_at": "2026-11-24T10:01:00+00:00", "turn_outcome": "completed"},
        {"session_id": "s", "turn_id": "t", "answer_total_tokens": 1600, "model": "m",
         "created_at": "2026-11-24T10:02:00+00:00", "turn_outcome": "completed"},
        {"session_id": "s", "agent_id": "child", "turn_id": "t", "answer_total_tokens": 50},
        {"session_id": "s", "turn_id": None, "answer_total_tokens": 7},
        {"session_id": "s", "turn_id": None, "answer_total_tokens": 7},
    ]
    turns = dashboard.collapse_turns(rows)
    assert [(thread, view["tokens"]) for thread, view in turns] == [
        ("s", 1600), ("child", 50), ("s", 7), ("s", 7)]
    assert turns[0][1]["outcome"] == "completed"


def test_snapshot_joins_threads_routes_goals_waits_and_packets(state):
    snapshot = state.snapshot()
    agents = {entry["id"]: entry for entry in snapshot["agents"]}
    assert set(agents) == {ROOT, WORKER, WAITER}
    assert snapshot["summary"]["reviewers"] == {
        "count": 1, "tokens": 40, "by_kind": {"review": 1}}
    assert snapshot["summary"]["unused_threads"] == 1

    root = agents[ROOT]
    assert (root["state"], root["title"]) == ("working", "AgentRoute live agent dashboard")
    assert root["cwd"] == "~/build/agent-router"
    assert root["model"] == "claude-fable-5-2"  # The latest route, not the stored thread model.
    assert (root["tokens_window"], root["turns_window"]) == (1600, 2)
    assert root["goal"]["status"] == "active"
    assert root["children"] == [WORKER]
    assert root["packets"] == [{"namespace": "private", "packet": "agentroute-dashboard",
                                "item": "build", "status": "active",
                                "next_action": "Write tests"}]
    activity = root["activity"]
    assert activity["tools"] == {"shell": 2, "apply_patch": 1, "mcp:contactdb": 1}
    assert activity["programs"] == {"gh": 1, "uv": 1}
    assert activity["failures"] == 1
    assert activity["message"]["text"] == "Wiring the CLI next."
    assert activity["tokens"]["context_used"] == 300
    targets = {(target["kind"], target["label"]) for target in activity["targets"]}
    assert {("github", "atiti/agent-router"), ("pr", "atiti/agent-router#204"),
            ("mcp", "contactdb"), ("dir", "~/build/agent-router")} <= targets
    assert "ghp_" not in json.dumps(snapshot)

    worker = agents[WORKER]
    assert (worker["kind"], worker["parent"]) == ("worker", ROOT)
    assert worker["title"] == "Laplace · state_sources"
    assert worker["tokens_window"] == 700
    assert agents[WAITER]["state"] == "waiting"
    assert "w-11111111 command" in agents[WAITER]["state_reason"]

    assert snapshot["summary"]["working"] == 1 and snapshot["summary"]["waiting"] == 1
    assert snapshot["summary"]["pending_waits"] == 1
    assert (snapshot["usage"]["tokens"], snapshot["usage"]["turns"]) == (2300, 3)
    assert [wait["id"] for wait in snapshot["waits"]] == ["w-11111111", "w-22222222"]
    assert "abc123" not in snapshot["waits"][0]["note"]
    packet = snapshot["packets"][0]
    assert (packet["namespace"], packet["id"]) == ("private", "agentroute-dashboard")
    assert packet["items"][0]["criteria_met"] == 1 and packet["items"][0]["criteria_total"] == 2
    assert packet["items"][0]["gates"] == {"ci": "pending"}
    assert snapshot["summary"]["open_packet_items"] == 1
    assert snapshot["sources"] == {
        "codex_threads": True, "codex_goals": True, "routing_audit": True, "herdr": False,
        "shared_owner": False, "work_packets": True}
    focus = snapshot["focus"]
    assert focus["names"] == ["Ana"] and focus["counts"]["needs_you"] == 0
    assert focus["claims"] == [{"namespace": "private", "claimed": 1, "limit": 20, "held": 0,
                                "stale": 0, "ready": 0}]


def test_agent_detail_lists_events_turns_and_wait_history(state):
    detail = state.agent(ROOT)
    assert [entry["tool"] for entry in detail["events"]] == [
        "mcp:contactdb", "apply_patch", "shell", "shell"]
    assert detail["events"][2]["ok"] is False
    assert [(turn["model"], turn["tokens"]) for turn in detail["turns"]] == [
        ("claude-fable-5-2", None), ("gpt-6.2-sol", 1600)]
    assert [wait["id"] for wait in detail["wait_history"]] == ["w-22222222"]
    assert state.agent("missing") is None


def test_snapshot_is_empty_but_valid_without_any_state():
    snapshot = dashboard.Collector().snapshot()
    assert snapshot["agents"] == [] and snapshot["waits"] == []
    assert not any(snapshot["sources"][name] for name in ("codex_threads", "routing_audit"))


def test_changed_sections_sends_each_section_once_until_it_changes(state):
    snapshot, sent = state.snapshot(), {}
    assert set(dashboard_server.changed_sections(snapshot, sent)) == \
        set(dashboard_server.SECTIONS)
    assert dashboard_server.changed_sections(snapshot, sent) == {}
    changed = dashboard_server.changed_sections(
        {**snapshot, "summary": {**snapshot["summary"], "working": 9}}, sent)
    assert list(changed) == ["summary"]


@pytest.fixture
def served(state):
    server = dashboard_server.make_server("127.0.0.1", 0, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def get(url: str, host: str | None = None):
    request = urllib.request.Request(url, headers={"Host": host} if host else {})
    return urllib.request.urlopen(request, timeout=5)


def test_server_serves_the_page_and_json(served):
    page = get(served + "/")
    assert page.headers["Content-Type"].startswith("text/html")
    assert b"<title>AgentRoute</title>" in page.read()
    snapshot = json.load(get(served + "/api/snapshot?window=3600"))
    assert snapshot["window"] == 3600
    assert json.load(get(f"{served}/api/agent/{ROOT}"))["id"] == ROOT
    for path in ("/api/agent/unknown", "/elsewhere"):
        with pytest.raises(urllib.error.HTTPError) as error:
            get(served + path)
        assert error.value.code == 404


def test_server_refuses_a_request_addressed_to_another_host(served):
    with pytest.raises(urllib.error.HTTPError) as error:
        get(served + "/api/snapshot", host="attacker.example")
    assert error.value.code == 403
    assert json.load(get(served + "/api/snapshot", host="localhost:1234"))["agents"]


def test_server_streams_the_snapshot_as_an_event(served):
    with get(served + "/api/events") as stream:
        assert stream.headers["Content-Type"] == "text/event-stream"
        line = stream.readline()
    assert line.startswith(b"data: ")
    message = json.loads(line[6:])
    assert set(dashboard_server.SECTIONS) <= set(message)


def test_dashboard_command_reports_a_port_it_cannot_use(monkeypatch):
    def refuse(host, port, open_browser, names=None):
        raise OSError(48, "Address already in use")

    monkeypatch.setattr(dashboard_server, "serve", refuse)
    result = CliRunner().invoke(app, ["dashboard", "--no-open"])
    assert result.exit_code == 1
    assert "8719" in result.output and "--port" in result.output


def outcome(status="active", step=None, wait=None, later=None, blocker=None, **extra):
    """A tracker outcome with the given current step state, its wait and a later hold."""
    item = {"id": "x", "status": status, "blocker": blocker, **extra}
    if step:
        item["next_step"] = {"id": "s", "state": step, **({"wait": wait} if wait else {})}
    if later:
        item["outcome_wait"] = later
    return item


def wait(owner, reason="because", resume="when it is done", **extra):
    return {"owner": owner, "reason": reason, "resume_when": resume, **extra}


@pytest.mark.parametrize(("item", "kind", "blocking"), [
    (outcome("blocked", "awaiting_user", wait("Ana", question_ref="chat:q1")), "you", True),
    # Recorded as an outside wait, but the named owner is the reader.
    (outcome("ready", "waiting_external", wait("ana and the Reports owner")), "you", True),
    (outcome("ready", "waiting_external", wait("GitHub Actions")), "external", True),
    (outcome("ready", "waiting_worker", wait("Ivan supervisor")), "worker", True),
    (outcome("blocked", "coordination_required", wait("root coordinator")), "coordination", True),
    # A name inside another word is not that person.
    (outcome("ready", "waiting_external", wait("Banana supplier")), "external", True),
    # Free text only: nobody is named, so nobody will be prompted.
    (outcome("blocked", blocker="Router owner decision is pending"), "unowned", True),
    # Work can continue now; the approval is needed before the outcome closes.
    (outcome("active", "active", later={"state": "awaiting_user", **wait("Ana")}), "you", False),
    (outcome("blocked", later={"state": "waiting_external", **wait("Provider")}), "external", True),
])
def test_item_hold_names_who_must_act_from_structured_fields(item, kind, blocking):
    hold = dashboard.item_hold(item, ["Ana"])
    assert (hold["kind"], hold["blocking"]) == (kind, blocking)


@pytest.mark.parametrize("item", [
    outcome("active", "active"),
    outcome("ready", "ready"),
    outcome("blocked", "ready", blocker="later approval"),  # A step is executable now.
    outcome("done", "awaiting_user", wait("Ana")),
    outcome("cancelled", blocker="abandoned"),
])
def test_item_hold_is_absent_when_work_can_proceed_or_is_finished(item):
    assert dashboard.item_hold(item, ["Ana"]) is None


def test_item_hold_carries_the_reason_and_the_condition_that_clears_it():
    hold = dashboard.item_hold(outcome(
        "blocked", "awaiting_user", blocker="older prose",
        wait=wait("Ana", "Production upgrade is not approved; token=abc123secret",
                  "Explicit approval of the window", question_ref="chat:q7")), ["Ana"])
    assert hold["unblock"] == "Explicit approval of the window"
    assert hold["question"] == "chat:q7"
    assert "not approved" in hold["reason"] and "abc123secret" not in hold["reason"]
    legacy = dashboard.item_hold(outcome("blocked", blocker="Decision pending"), [])
    assert (legacy["reason"], legacy["unblock"], legacy["owner"]) == ("Decision pending", "", "")


def packet_item(item_id, hold=None, session=None, status="active", priority="P1",
                checkpoint_at=NOW - 60, review_at=None):
    return {"id": item_id, "objective": f"Outcome {item_id}", "status": status,
            "priority": priority, "next_action": "next", "session": session, "hold": hold,
            "checkpoint_at": checkpoint_at, "review_at": review_at}


def hold(kind, blocking=True, owner="", reason="r", unblock="u"):
    return {"kind": kind, "blocking": blocking, "owner": owner, "reason": reason,
            "unblock": unblock, "question": ""}


def test_focus_separates_what_needs_the_reader_from_what_is_stuck_elsewhere():
    old = NOW - 5 * 86400
    packets = [
        {"namespace": "markster", "id": "p1", "updated_at": NOW, "items": [
            packet_item("approve", hold("you", owner="Ana"), priority="P2", checkpoint_at=old,
                        review_at=NOW - 86400),
            packet_item("urgent", hold("you"), priority="P0", session=ROOT),
            packet_item("later", hold("you", blocking=False), priority="P0"),
            packet_item("ci", hold("external", owner="GitHub Actions"), session="gone",
                        checkpoint_at=old),
            packet_item("peer", hold("worker", owner="Ivan")),
            packet_item("prose", hold("unowned"), status="blocked"),
            packet_item("finished", hold("you"), status="done"),
            packet_item("free", status="ready"),
            packet_item("moving", session=WORKER),
        ]},
        {"namespace": "private", "id": "p2", "updated_at": NOW, "items": [
            packet_item(f"c{n}", session=f"s{n}") for n in range(dashboard.CLAIM_LIMIT)]},
    ]
    agents = [
        {"id": ROOT, "title": "Deploy", "state": "needs_input",
         "state_reason": "waiting for you to approve an action", "active_at": NOW - 30,
         "pane": {"pane_id": "w1:p1", "workspace": "Ops"}, "goal": None, "task": None,
         "activity": {"message": {"text": "Run the migration?"}}},
        {"id": WORKER, "title": "Build", "state": "working", "state_reason": "turn in progress",
         "active_at": NOW, "pane": None, "goal": None, "task": None, "activity": {}},
        {"id": WAITER, "title": "Budget", "state": "blocked", "state_reason": "goal budget limited",
         "active_at": NOW - 900, "pane": None, "goal": {"status": "budget_limited"},
         "task": {"summary": "Halfway through the import"}, "activity": {}},
    ]
    failed = [{"id": "w-9", "thread_id": WAITER, "state": "undeliverable",
               "description": "w-9 pr a/b#1", "deadline_at": NOW - 10, "created_at": NOW - 99}]
    focus = dashboard.build_focus(packets, agents, failed, ["Ana"], NOW)

    assert [(e["type"], e.get("item") or e["title"]) for e in focus["needs_you"]] == [
        ("agent", "Deploy"), ("outcome", "urgent"), ("outcome", "approve"), ("outcome", "later")]
    approve = focus["needs_you"][2]
    assert approve["review_overdue"] and approve["agent"] is None
    assert focus["needs_you"][1]["agent"] == ROOT
    deploy = focus["needs_you"][0]
    assert (deploy["pane"], deploy["detail"]) == ("w1:p1", "Run the migration?")
    groups = {g["kind"]: g["items"] for g in focus["others"]}
    assert [e["title"] for e in groups["coordination"]] == ["Budget", "w-9 pr a/b#1"]
    assert "budget" in groups["coordination"][0]["unblock"]
    assert [e["item"] for e in groups["worker"]] == ["peer"]
    assert [e["item"] for e in groups["external"]] == ["ci"]
    assert [e["item"] for e in focus["unowned"]] == ["prose"]
    assert focus["counts"] == {"needs_you": 4, "unowned": 1, "others": 4, "blocking_you": 3}
    assert focus["claims"] == [
        {"namespace": "private", "claimed": 20, "limit": 20, "held": 0, "stale": 0, "ready": 0},
        {"namespace": "markster", "claimed": 3, "limit": 20, "held": 2, "stale": 1, "ready": 1}]


@pytest.mark.parametrize(("live", "turn", "expected"), [
    ("approval", None, "needs_input"),
    ("input", {"tools": 1}, "needs_input"),
    ("running", None, "working"),          # The rollout has not caught up yet.
    ("idle", {"tools": 3}, "idle"),        # The turn died with its process.
    ("unknown", {"tools": 3}, "idle"),     # Not loaded by the owner, so nothing is running.
    ("error", None, "blocked"),
    (None, {"tools": 3}, "working"),       # Owner unreachable: judge from the rollout.
])
def test_agent_state_takes_the_shared_owner_as_the_authority(live, turn, expected):
    state = dashboard.agent_state(agent(live=live, activity={"turn": turn, "last_turn": None}),
                                  NOW)
    assert state[0] == expected


def test_owner_states_reads_each_loaded_thread(monkeypatch):
    from agentroute import shared_server

    statuses = {
        "a": {"type": "active", "activeFlags": ["waitingOnApproval"]},
        "b": {"type": "active", "activeFlags": ["waitingOnUserInput"]},
        "c": {"type": "active", "activeFlags": []}, "d": {"type": "idle"},
        "e": {"type": "systemError"}, "f": {"type": "notLoaded"}}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, payload):
            if method == "thread/loaded/list":
                return {"data": list(statuses)}
            return {"thread": {"status": statuses[payload["threadId"]]}}

    monkeypatch.setattr(shared_server, "RpcClient", Client)
    assert dashboard.owner_states() == {"a": "approval", "b": "input", "c": "running",
                                        "d": "idle", "e": "error", "f": "unknown"}
    monkeypatch.setattr(shared_server, "RpcClient", Mock(side_effect=OSError("no socket")))
    assert dashboard.owner_states() is None


def test_herdr_panes_reads_one_session_and_prefers_the_identity_herdr_holds(monkeypatch):
    import hashlib

    home = Path(os.environ["AGENTROUTE_HOME"])
    monkeypatch.setenv("HERDR_SESSION", "work")

    def record(session, pane, thread):
        key = hashlib.sha256(session.encode()).hexdigest()[:16]
        directory = home / "shared-server" / "abc" / "herdr-layout" / key
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{pane.replace(':', '-')}.json").write_text(
            json.dumps({"pane_id": pane, "thread": thread, "terminal_id": "term-" + pane}))

    record("work", "w1:p1", ROOT)
    record("work", "w2:p1", WORKER)
    record("work", "w4:p1", WAITER)
    record("other", "w3:p1", WAITER)  # The same pane ID in another Herdr session.
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        result = {"agents": [
            {"pane_id": "w1:p1", "terminal_id": "term-w1:p1", "workspace_id": "w1",
             "agent_status": "working"},
            {"pane_id": "w4:p1", "terminal_id": "replacement", "workspace_id": "w1"},
            {"pane_id": "w2:p1", "workspace_id": "w1", "agent_status": "blocked",
             "agent_session": {"source": "herdr:codex", "kind": "id", "value": REVIEW}},
            {"pane_id": "w3:p1", "workspace_id": "w1", "agent_status": "idle"},
        ]} if argv[3:5] == ["agent", "list"] else {
            "workspaces": [{"workspace_id": "w1", "label": "Ops"}]}
        return SimpleNamespace(stdout=json.dumps({"result": result}))

    monkeypatch.setattr(dashboard.subprocess, "run", run)
    panes = dashboard.herdr_panes()
    assert all(argv[:3] == ["herdr", "--session", "work"] for argv in calls)
    assert set(panes) == {ROOT, REVIEW}
    assert panes[REVIEW] == {"pane_id": "w2:p1", "workspace": "Ops", "status": "blocked",
                             "title": None, "focused": False}


def test_names_person_matches_whole_names_only():
    assert dashboard.names_person("Ana and the Reports owner", ["ana"])
    assert dashboard.names_person("root coordinator / ANA", ["Ana"])
    assert not dashboard.names_person("Banana supplier", ["Ana"])
    assert not dashboard.names_person("GitHub Actions", [])
