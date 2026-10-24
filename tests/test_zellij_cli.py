import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agentroute import zellij_cli as z

THREAD = "01a16cea-b69e-7772-b0c2-77d9a04435c0"


def test_status_distinguishes_approval_input_and_idle():
    assert z.status({"status": {"type": "active"}}) == "RUNNING"
    approval = {"type": "active", "activeFlags": ["waitingOnApproval"]}
    user_input = {"type": "active", "activeFlags": ["waitingOnUserInput"]}
    assert z.status({"status": approval}) == "APPROVAL"
    assert z.status({"status": user_input}) == "INPUT"
    assert z.status({"status": {"type": "idle"}}) == "READY"
    assert z.status({"status": {"type": "notLoaded"}}) == "SAVED"


def test_recent_list_includes_older_loaded_threads_without_resuming():
    client = Mock()
    client.request.side_effect = [
        {"data": [{"id": "recent"}]},
        {"data": ["old"], "nextCursor": None},
        {"thread": {"id": "old", "status": {"type": "active"}}},
    ]
    assert [t["id"] for t in z.sessions(client)] == ["recent", "old"]
    assert [c.args[0] for c in client.request.call_args_list] == [
        "thread/list", "thread/loaded/list", "thread/read"
    ]


def test_pane_discovery_uses_immutable_command_not_title():
    command = f"python -m agentroute.cli zellij client --thread {THREAD}"
    valid = {"id": 2, "terminal_command": command, "title": "Changed by Codex"}
    assert z.thread_panes([valid, {**valid, "id": 3, "exited": True},
                           {**valid, "id": 4, "is_plugin": True}]) == {THREAD: valid}


def test_open_existing_pane_does_not_launch_another_writer(monkeypatch):
    monkeypatch.setattr(z, "operation_lock", __import__("contextlib").nullcontext)
    monkeypatch.setattr(z, "panes", lambda _: [{
        "id": 7, "terminal_command": f"python -m agentroute.cli zellij client --thread {THREAD}"
    }])
    action = Mock(return_value="")
    monkeypatch.setattr(z, "action", action)
    assert z.open_thread({"id": THREAD}, "test") == "terminal_7"
    action.assert_called_once_with("test", "focus-pane-id", "terminal_7")


def test_tiling_retains_all_terminals_and_plugins(monkeypatch):
    monkeypatch.setattr(z, "panes", lambda _: [
        {"id": 0, "tab_id": 1, "terminal_command": "python -m agentroute.cli zellij board"},
        {"id": 1, "tab_id": 1, "is_focused": True},
        {"id": 2, "tab_id": 1},
        {"id": 3, "tab_id": 2},
    ])
    action = Mock()
    monkeypatch.setattr(z, "action", action)
    z.tile_panes("test")
    action.assert_called_once_with("test", "next-swap-layout", "--tab-id", "1")


def test_attention_transitions_do_not_alert_for_initial_idle(monkeypatch):
    notify = Mock()
    monkeypatch.setattr(z, "notify", notify)
    thread = {"id": THREAD, "status": {"type": "idle"}}
    mapped = {THREAD: {"id": 1}}
    previous = z.notify_changes([thread], mapped, {}, "test")
    notify.assert_not_called()
    thread["status"] = {"type": "active"}
    previous = z.notify_changes([thread], mapped, previous, "test")
    assert notify.call_args.args[-1] == "done"
    thread["status"] = {"type": "active", "activeFlags": ["waitingOnApproval"]}
    previous = z.notify_changes([thread], mapped, previous, "test")
    assert notify.call_args.args[-1] == "alert"
    notify.reset_mock()
    z.notify_changes([thread], mapped, previous, "test")
    notify.assert_not_called()


def test_hook_target_has_outer_terminal_and_exact_pane(tmp_path, monkeypatch):
    monkeypatch.setattr(z, "state_dir", lambda: tmp_path)
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "test")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "4")
    z.host_path("test").write_text(json.dumps({"terminal": "ghostty", "tty": "/dev/ttys012"}))
    path = z.register_target(THREAD)
    assert json.loads(path.read_text()) == {
        "session": "test", "pane_id": 4, "terminal": "ghostty", "tty": "/dev/ttys012",
        "thread": THREAD,
    }
    assert path.stat().st_mode & 0o777 == 0o600


def test_ambiguous_title_refuses_to_guess(monkeypatch):
    monkeypatch.setattr(z, "sessions", lambda *_: [
        {"id": "one", "name": "Fix billing"}, {"id": "two", "name": "Fix billing again"}
    ])
    with pytest.raises(z.SharedServerError, match="found 2"):
        z.resolve(Mock(), "billing")


def test_install_preserves_unrelated_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(z, "agentroute_home", lambda: tmp_path / "route")
    z.install_command(bin_dir=tmp_path / "bin")
    script = tmp_path / "bin" / "ar"
    assert 'set -- start' in script.read_text()
    assert script.stat().st_mode & 0o777 == 0o755
    script.write_text("unrelated tool")
    with pytest.raises(__import__("typer").BadParameter, match="unrelated command"):
        z.install_command(bin_dir=tmp_path / "bin")
    assert script.read_text() == "unrelated tool"


def test_shared_owner_does_not_inherit_the_first_panes_focus_address(tmp_path, monkeypatch):
    from agentroute.config import agentroute_home
    from agentroute.shared_server import server_environment

    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path))
    monkeypatch.setattr("agentroute.storage.prepare_capture", lambda _: None)
    assert agentroute_home() == tmp_path
    result = server_environment({"ZELLIJ_SESSION_NAME": "first", "ZELLIJ_PANE_ID": "1",
                                 "OVERWATCHR_TTY": "/dev/ttys001", "PATH": "/bin"})
    assert "ZELLIJ_SESSION_NAME" not in result
    assert "ZELLIJ_PANE_ID" not in result
    assert "OVERWATCHR_TTY" not in result
    assert result["PATH"] == "/bin"


def test_focus_accepts_a_positional_chat_or_board(monkeypatch):
    from typer.testing import CliRunner

    monkeypatch.setattr(z, "panes", lambda _: [
        {"id": 0, "terminal_command": "python -m agentroute.cli zellij board"}
    ])
    action = Mock(return_value="")
    monkeypatch.setattr(z, "action", action)
    result = CliRunner().invoke(z.zellij_app, ["focus", "board", "--session", "test"])
    assert result.exit_code == 0, result.output
    action.assert_called_once_with("test", "focus-pane-id", "terminal_0")


def _stub_start(monkeypatch, tmp_path):
    settings = SimpleNamespace(shared_server=SimpleNamespace(enabled=True))
    monkeypatch.setattr(z, "_start", Mock())
    monkeypatch.setattr(z, "load_config", lambda: settings)
    monkeypatch.setattr(z, "save_config", Mock())
    monkeypatch.setattr(z, "remember_host", Mock())
    monkeypatch.setattr(z, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(z, "executable", lambda _: "/opt/homebrew/bin/zellij")
    return settings


def test_start_reuses_an_existing_running_session(monkeypatch, tmp_path):
    _stub_start(monkeypatch, tmp_path)
    run = Mock(return_value=SimpleNamespace(
        returncode=0, stdout="agentroute [Created 1m ago]\n", stderr=""
    ))
    monkeypatch.setattr(z.subprocess, "run", run)

    z.start_command(background=True)

    run.assert_called_once_with(
        ["/opt/homebrew/bin/zellij", "list-sessions", "--no-formatting"],
        capture_output=True, text=True, timeout=15,
    )


def test_start_resurrects_an_exited_session_with_saved_commands(monkeypatch, tmp_path):
    _stub_start(monkeypatch, tmp_path)
    run = Mock(return_value=SimpleNamespace(
        returncode=0,
        stdout="agentroute [Created 1m ago] (EXITED - attach to resurrect)\n",
        stderr="",
    ))
    execvp = Mock()
    monkeypatch.setattr(z.subprocess, "run", run)
    monkeypatch.setattr(z.os, "execvp", execvp)
    monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)

    z.start_command()

    execvp.assert_called_once_with("zellij", [
        "zellij", "--config", str(tmp_path / "zellij.kdl"), "attach",
        "--force-run-commands", "agentroute",
    ])


def test_start_creates_a_board_only_when_the_session_is_missing(monkeypatch, tmp_path):
    _stub_start(monkeypatch, tmp_path)
    run = Mock(side_effect=[
        SimpleNamespace(returncode=0, stdout="", stderr=""),
        SimpleNamespace(returncode=0, stdout="", stderr=""),
    ])
    monkeypatch.setattr(z.subprocess, "run", run)

    z.start_command(background=True)

    assert run.call_count == 2
    create_args = run.call_args_list[1].args[0]
    assert create_args[create_args.index("attach") + 1:create_args.index("--")] == [
        "--create-background", "agentroute",
    ]
    assert create_args[-3:] == ["board", "--session", "agentroute"]
