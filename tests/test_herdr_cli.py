import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agentroute import herdr_cli as h
from agentroute.shared_server import SharedServerError

THREAD = "11111111-1111-1111-1111-111111111111"


def test_mutations_accept_empty_ack_and_surface_json_errors(monkeypatch):
    monkeypatch.setattr(h.chats, "executable", lambda _: "herdr")
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(h.subprocess, "run", run)
    assert h.command("named", "pane", "run", "w1:p1", "echo hello") == {}
    assert run.call_args.args[0][:3] == ["herdr", "--session", "named"]
    run.return_value.stdout = '{"error":{"code":"pane_not_found"}}'
    with pytest.raises(SharedServerError, match="pane_not_found"):
        h.command("named", "pane", "get", "missing")


def test_registry_uses_live_process_and_terminal_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "state_dir", lambda: tmp_path)
    target = h.registry_path("test", THREAD)
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"pid": os.getpid(), "terminal_id": "terminal-a"}))
    moved = {"terminal_id": "terminal-a", "pane_id": "w2:p3"}
    assert h.thread_panes([moved], "test") == {THREAD: moved}
    assert h.thread_panes([{"terminal_id": "other", "pane_id": "w2:p3"}], "test") == {}
    assert h.thread_panes([moved], "another-session") == {}
    monkeypatch.setattr(h.os, "kill", Mock(side_effect=ProcessLookupError))
    assert h.thread_panes([moved], "test") == {}


def test_open_existing_only_focuses_without_starting_another_writer(monkeypatch):
    pane = {"pane_id": "w1:p1", "terminal_id": "t", "tab_id": "w1:t1",
            "workspace_id": "w1"}
    monkeypatch.setattr(h, "panes", lambda _: [pane])
    monkeypatch.setattr(h, "thread_panes", lambda *_: {THREAD: pane})
    from contextlib import nullcontext
    monkeypatch.setattr(h.chats, "operation_lock", nullcontext)
    command = Mock()
    monkeypatch.setattr(h, "command", command)
    assert h.open_thread({"id": THREAD}, "test") == "w1:p1"
    assert [c.args[1:3] for c in command.call_args_list] == [
        ("workspace", "focus"), ("tab", "focus"), ("agent", "focus")]


def test_install_refuses_unrelated_focus_before_replacing_ar(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "agentroute_home", lambda: tmp_path)
    (tmp_path / "ar").write_text("# AgentRoute Zellij cockpit\nold")
    (tmp_path / "focus").write_text("user script")
    import typer
    with pytest.raises(typer.BadParameter, match="unrelated"):
        h.install_command(tmp_path)
    assert (tmp_path / "ar").read_text().endswith("old")


def test_launcher_reports_and_releases_even_when_client_fails(monkeypatch):
    command = Mock()
    monkeypatch.setattr(h, 'command', command)
    monkeypatch.setattr(h.subprocess, 'run', Mock(return_value=SimpleNamespace(returncode=7)))
    assert h.run_in_pane(['codex-bin'], {'HERDR_PANE_ID': 'w8:p2'}) == 7
    assert command.call_args_list[0].args[1:3] == ('pane', 'report-agent')
    assert command.call_args_list[-1].args[1:3] == ('pane', 'release-agent')


def test_direct_resume_reports_shared_thread_state(monkeypatch):
    thread = "01a20744-50ee-71a2-a6a3-3e26905fc41b"
    reports = []
    def command(session, *args):
        reports.append(args)
        if args[:2] == ("pane", "get"):
            return {"pane": {}}
    monkeypatch.setattr(h, "command", command)
    monkeypatch.setattr(h, "socket_path", lambda: "/tmp/test.sock")

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, payload):
            assert method == "thread/read"
            assert payload["threadId"] == thread
            return {"thread": {"status": {"type": "active"}}}

    monkeypatch.setattr(h, "RpcClient", Client)
    monkeypatch.setattr(h.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    assert h.run_in_pane(
        ["codex-bin", "resume", thread],
        {"HERDR_PANE_ID": "w8:p2", "AGENTROUTE_LOCAL_SERVER_SOCKET": "/tmp/test.sock"},
    ) == 0
    assert any("--state" in row and "working" in row and thread in row for row in reports)
    assert reports[-1][:2] == ("pane", "release-agent")


def test_direct_resume_follows_native_session_identity(monkeypatch, tmp_path):
    old = "01a20744-50ee-71a2-a6a3-3e26905fc41b"
    new = "01a20db2-e37d-7ad3-8004-8cb3ee6bdd81"
    reports = []
    pane = {"pane_id": "w8:p2", "workspace_id": "w8", "tab_id": "w8:t1",
            "terminal_id": "term-active", "cwd": "/work"}

    def command(session, *args):
        reports.append(args)
        if args[:2] == ("pane", "get"):
            return {"pane": {**pane, "agent_session": {
                "source": "herdr:codex", "kind": "id",
                "value": old if len(reports) == 1 else new,
            }}}

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def request(self, method, payload):
            assert payload["threadId"] == new
            return {"thread": {"status": {"type": "active", "activeFlags": ["waitingOnUserInput"]}}}

    monkeypatch.setattr(h, "command", command)
    monkeypatch.setattr(h, "socket_path", lambda: "/tmp/test.sock")
    monkeypatch.setattr(h, "RpcClient", Client)
    monkeypatch.setattr(h, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(h.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=-1))
    assert h.run_in_pane(
        ["codex-bin", "resume", old],
        {"HERDR_PANE_ID": "w8:p2", "AGENTROUTE_LOCAL_SERVER_SOCKET": "/tmp/test.sock"},
    ) == -1
    assert any("--state" in row and "blocked" in row and new in row for row in reports)
    assert list(h.saved_panes(h.SESSION)) == [{**pane, "thread": new}]



def test_restore_reuses_positions_and_skips_occupied_or_live_threads(tmp_path, monkeypatch):
    from contextlib import nullcontext

    monkeypatch.setattr(h, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(h.chats, "operation_lock", nullcontext)
    originals = [
        {"pane_id": f"w1:p{n}", "workspace_id": "w1", "tab_id": f"w1:t{n}",
         "terminal_id": f"old-{n}", "cwd": "/work"}
        for n in range(1, 6)
    ]
    ids = [f"11111111-1111-1111-1111-{n:012}" for n in range(1, 6)]
    for pane, thread in zip(originals, ids, strict=True):
        h.remember_pane("work", pane, thread)

    current = [
        {**originals[0], "terminal_id": "new-1"},
        {**originals[1], "terminal_id": "new-2"},
        {**originals[2], "terminal_id": "new-3"},
        {**originals[3], "terminal_id": "new-4", "tab_id": "w1:t8"},
        {**originals[4], "agent": "codex"},
    ]
    monkeypatch.setattr(h, "panes", lambda _: current)
    tracked = {ids[2]: current[-1], ids[4]: current[-1]}
    monkeypatch.setattr(h, "thread_panes", lambda *_: tracked)

    def command(session, *args):
        assert args[:3] == ("pane", "process-info", "--pane")
        occupied = args[3] == "w1:p2"
        return {"process_info": {"shell_pid": 10,
                                 "foreground_process_group_id": 20 if occupied else 10,
                                 "foreground_processes": [{"name": "vim" if occupied else "zsh"}]}}

    monkeypatch.setattr(h, "command", command)
    launch = Mock(side_effect=lambda session, pane, thread: tracked.update({thread: current[0]}))
    monkeypatch.setattr(h, "launch_client", launch)
    rows = {row["pane"]: row["status"] for row in h.restore_panes("work")}
    launch.assert_called_once_with("work", "w1:p1", ids[0])
    assert rows == {
        "w1:p1": "started", "w1:p2": "occupied",
        "w1:p3": "already present or original terminal still open",
        "w1:p4": "missing or moved",
        "w1:p5": "already present or original terminal still open",
    }
    assert list(h.saved_panes("other")) == []



def test_bare_chat_is_named_and_persisted_before_resume(monkeypatch, tmp_path):
    client = Mock()
    client.request.side_effect = [{"thread": {"id": THREAD}}, {}]

    class Client:
        def __enter__(self):
            return client

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(h, "RpcClient", lambda *args, **kwargs: Client())
    monkeypatch.chdir(tmp_path)
    endpoint = tmp_path / "owner.sock"
    assert h.track_new_chat(["codex-bin"], endpoint) == ["codex-bin", "resume", THREAD]
    assert [call.args for call in client.request.call_args_list] == [
        ("thread/start", {"cwd": str(tmp_path.resolve())}),
        ("thread/name/set", {"threadId": THREAD, "name": "New chat"}),
    ]



def test_restore_shortcut_routes_to_restore_and_attaches(monkeypatch):
    from typer.testing import CliRunner

    restore = Mock()
    monkeypatch.setattr(h, "restore_command", restore)
    result = CliRunner().invoke(h.herdr_app, ["r", "--session", "test"])
    assert result.exit_code == 0, result.output
    restore.assert_called_once_with(session="test", attach=True)
