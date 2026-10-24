import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from agentroute import zellij_cli as z
from agentroute.config import (
    ZellijConfig,
    ZellijNamespaceConfig,
    default_config,
    load_config,
    save_config,
)

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


def test_project_groups_choose_markster_repositories_and_private_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(z, "agentroute_home", lambda: tmp_path / "agentroute")
    work = tmp_path / "build" / "markster-core"
    private = tmp_path / "build" / "ridgify"
    work.mkdir(parents=True)
    private.mkdir(parents=True)

    assert z.project_group(work) == "Markster"
    assert z.project_group(private) == "Private"


def test_project_group_overrides_use_the_most_specific_root(tmp_path, monkeypatch):
    monkeypatch.setattr(z, "agentroute_home", lambda: tmp_path / "agentroute")
    root = tmp_path / "workspace"
    nested = root / "private-project"
    nested.mkdir(parents=True)
    z.set_project_group(root, "markster")
    z.set_project_group(nested, "private")

    assert z.project_group(root / "a-chat") == "Markster"
    assert z.project_group(nested / "a-chat") == "Private"
    assert z.project_groups_path().stat().st_mode & 0o777 == 0o600


def test_project_group_reads_markster_identity_from_local_git_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(z, "agentroute_home", lambda: tmp_path / "agentroute")
    project = tmp_path / "general-name"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run([
        "git", "-C", str(project), "remote", "add", "origin",
        "git@github.com:markster-io/internal-tool.git",
    ], check=True)

    assert z.project_group(project) == "Markster"


def test_configured_namespace_roots_git_remotes_and_default(tmp_path, monkeypatch):
    markster = tmp_path / "work" / "markster"
    personal = tmp_path / "work" / "personal"
    markster.mkdir(parents=True)
    personal.mkdir()
    config = ZellijConfig(
        default_namespace="Personal",
        namespaces={
            "Markster": ZellijNamespaceConfig(
                project_roots=[str(markster)], git_remotes=["github.com/markster-io/*"]
            ),
            "Personal": ZellijNamespaceConfig(),
            "CabinPals": ZellijNamespaceConfig(project_roots=[str(personal)]),
        },
    )
    monkeypatch.setattr(z, "load_config", lambda: SimpleNamespace(zellij=config))
    monkeypatch.setattr(z, "load_project_groups", lambda: [])

    assert z.project_group(markster / "repo") == "Markster"
    assert z.configured_namespace(
        tmp_path, "git@github.com:markster-io/site.git", config
    ) == "Markster"
    assert z.project_group(personal / "repo") == "CabinPals"
    assert z.project_group(tmp_path / "unassigned") == "Personal"


def test_git_remote_configuration_accepts_ssh_style_patterns():
    config = ZellijConfig(namespaces={
        "Work": ZellijNamespaceConfig(git_remotes=["git@github.com:markster-io/*"]),
        "Private": ZellijNamespaceConfig(),
    })

    assert z.configured_namespace(
        Path("/unrelated"), "ssh://git@github.com/markster-io/site.git", config
    ) == "Work"


def test_zellij_namespace_config_round_trips_through_config_yaml(tmp_path):
    config = default_config()
    config.zellij = ZellijConfig(
        default_namespace="Personal",
        namespaces={
            "Personal": ZellijNamespaceConfig(project_roots=["~/build/private-project"]),
            "Work": ZellijNamespaceConfig(git_remotes=["github.com/example/work-*"]),
        },
    )
    path = tmp_path / "config.yaml"

    save_config(config, path)
    loaded = load_config(path)

    assert loaded.zellij.default_namespace == "Personal"
    assert loaded.zellij.namespaces["Personal"].project_roots == ["~/build/private-project"]
    assert loaded.zellij.namespaces["Work"].git_remotes == ["github.com/example/work-*"]


def test_zellij_namespace_names_trim_whitespace_and_reject_case_duplicates():
    config = ZellijConfig(
        default_namespace=" Personal ",
        namespaces={" Markster ": ZellijNamespaceConfig(), "Personal": ZellijNamespaceConfig()},
    )

    assert config.default_namespace == "Personal"
    assert list(config.namespaces) == ["Markster", "Personal"]
    with pytest.raises(ValueError, match="unique without regard to case"):
        ZellijConfig(namespaces={
            "Work": ZellijNamespaceConfig(), "work": ZellijNamespaceConfig()
        })


def test_scoped_sessions_add_namespace_and_accept_repeated_or_comma_filters(tmp_path, monkeypatch):
    roots = {name: tmp_path / name.casefold() for name in ("Markster", "Private", "CabinPals")}
    for root in roots.values():
        root.mkdir()
    config = ZellijConfig(
        namespaces={name: ZellijNamespaceConfig(project_roots=[str(root)])
                    for name, root in roots.items()}
    )
    monkeypatch.setattr(z, "load_config", lambda: SimpleNamespace(zellij=config))
    monkeypatch.setattr(z, "load_project_groups", lambda: [])
    threads = [{"id": name, "cwd": str(root / "repo")} for name, root in roots.items()]

    result = z.scoped_sessions(threads, ["markster, CabinPals"])

    assert [(thread["id"], thread["namespace"]) for thread in result] == [
        ("Markster", "Markster"), ("CabinPals", "CabinPals")
    ]
    with pytest.raises(z.SharedServerError, match="Unknown namespace 'Missing'"):
        z.scoped_sessions(threads, ["Missing"])


def test_list_command_scopes_json_to_configured_namespaces(tmp_path, monkeypatch):
    root = tmp_path / "markster"
    root.mkdir()
    config = ZellijConfig(namespaces={
        "Markster": ZellijNamespaceConfig(project_roots=[str(root)]),
        "Private": ZellijNamespaceConfig(),
    })
    monkeypatch.setattr(z, "load_config", lambda: SimpleNamespace(zellij=config))
    monkeypatch.setattr(z, "load_project_groups", lambda: [])
    monkeypatch.setattr(z, "sessions", lambda *_: [
        {"id": "work", "cwd": str(root / "repo")},
        {"id": "personal", "cwd": str(tmp_path / "private-repo")},
    ])

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    monkeypatch.setattr(z, "RpcClient", lambda *_args, **_kwargs: Client())
    from typer.testing import CliRunner

    result = CliRunner().invoke(z.zellij_app, ["list", "--namespace", "Markster", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [{
        "id": "work", "cwd": str(root / "repo"), "namespace": "Markster"
    }]


def test_resolve_can_disambiguate_a_chat_by_namespace(tmp_path, monkeypatch):
    roots = {"Markster": tmp_path / "markster", "Private": tmp_path / "private"}
    for root in roots.values():
        root.mkdir()
    config = ZellijConfig(namespaces={
        name: ZellijNamespaceConfig(project_roots=[str(root)])
        for name, root in roots.items()
    })
    monkeypatch.setattr(z, "load_config", lambda: SimpleNamespace(zellij=config))
    monkeypatch.setattr(z, "load_project_groups", lambda: [])
    monkeypatch.setattr(z, "sessions", lambda *_: [
        {"id": "work", "cwd": str(roots["Markster"]), "name": "Billing"},
        {"id": "personal", "cwd": str(roots["Private"]), "name": "Billing"},
    ])

    assert z.resolve(Mock(), "billing", ["Markster"]) == {
        "id": "work", "cwd": str(roots["Markster"]), "name": "Billing",
        "namespace": "Markster",
    }


def test_ensure_tab_reuses_or_creates_named_tabs(monkeypatch):
    action = Mock(side_effect=['[{"tab_id":7,"name":"Markster"}]',
                               '[{"tab_id":7,"name":"Markster"}]', "8"])
    monkeypatch.setattr(z, "action", action)

    assert z.ensure_tab("test", "Markster") == "7"
    assert z.ensure_tab("test", "Private") == "8"
    assert action.call_args_list == [
        call("test", "list-tabs", "--json"),
        call("test", "list-tabs", "--json"),
        call("test", "new-tab", "--name", "Private", "--no-focus"),
    ]


def test_workspace_setup_names_board_and_creates_group_tabs_without_moving_panes(monkeypatch):
    monkeypatch.setattr(z, "panes", lambda _: [{
        "id": 0, "tab_id": 1, "terminal_command": "python -m agentroute.cli zellij board",
    }])
    monkeypatch.setattr(z, "tabs", lambda _: [{"tab_id": 1, "name": "agentroute"}])
    action = Mock(side_effect=["", "2", "3"])
    monkeypatch.setattr(z, "action", action)

    z.setup_workspace_tabs("test")

    assert action.call_args_list == [
        call("test", "rename-tab-by-id", "1", "Sessions"),
        call("test", "new-tab", "--name", "Markster", "--no-focus"),
        call("test", "new-tab", "--name", "Private", "--no-focus"),
    ]


def test_open_existing_pane_does_not_launch_another_writer(monkeypatch):
    monkeypatch.setattr(z, "operation_lock", __import__("contextlib").nullcontext)
    monkeypatch.setattr(z, "panes", lambda _: [{
        "id": 7, "tab_id": 9,
        "terminal_command": f"python -m agentroute.cli zellij client --thread {THREAD}"
    }])
    action = Mock(return_value="")
    monkeypatch.setattr(z, "action", action)
    assert z.open_thread({"id": THREAD}, "test") == "terminal_7"
    assert action.call_args_list == [
        call("test", "go-to-tab-by-id", "9"),
        call("test", "focus-pane-id", "terminal_7"),
    ]


def test_move_existing_pane_uses_plugin_and_preserves_the_codex_client(monkeypatch, tmp_path):
    from contextlib import nullcontext

    plugin = tmp_path / "pane_mover.wasm"
    plugin.write_bytes(b"wasm")
    source = {
        "id": 7,
        "tab_id": 1,
        "terminal_command": f"python -m agentroute.cli zellij client --thread {THREAD}",
    }
    moved = {**source, "tab_id": 2}
    monkeypatch.setattr(z, "operation_lock", nullcontext)
    monkeypatch.setattr(z, "panes", Mock(side_effect=[[source], [moved]]))
    monkeypatch.setattr(z, "ensure_tab", Mock(return_value="2"))
    monkeypatch.setattr(z, "pane_mover_plugin_path", lambda: plugin)
    tile = Mock()
    monkeypatch.setattr(z, "tile_panes", tile)
    action = Mock(return_value="")
    monkeypatch.setattr(z, "action", action)

    result = z.move_thread_pane({"id": THREAD, "name": "Billing fix"}, "private", "test")

    assert result == "Moved 'Billing fix' to Private; focused terminal_7."
    z.ensure_tab.assert_called_once_with("test", "Private")
    action.assert_has_calls([
        call("test", "pipe", "--plugin", f"file:{plugin}", "--name", "agentroute-move-pane",
             "--floating-plugin", "true", "--plugin-title", "AgentRoute pane mover",
             "--", "7 2", timeout=90),
        call("test", "go-to-tab-by-id", "2"),
        call("test", "focus-pane-id", "terminal_7"),
    ])
    tile.assert_called_once_with("test", "2")


def test_move_closed_chat_refuses_to_create_a_new_codex_client(monkeypatch):
    from contextlib import nullcontext

    monkeypatch.setattr(z, "operation_lock", nullcontext)
    monkeypatch.setattr(z, "panes", lambda _: [])
    ensure_tab = Mock()
    monkeypatch.setattr(z, "ensure_tab", ensure_tab)

    with pytest.raises(z.SharedServerError, match="no open terminal pane"):
        z.move_thread_pane({"id": THREAD}, "Markster", "test")

    ensure_tab.assert_not_called()


def test_move_to_current_group_tab_only_focuses_the_existing_pane(monkeypatch):
    from contextlib import nullcontext

    pane = {"id": 7, "tab_id": 2,
            "terminal_command": f"python -m agentroute.cli zellij client --thread {THREAD}"}
    monkeypatch.setattr(z, "operation_lock", nullcontext)
    monkeypatch.setattr(z, "panes", lambda _: [pane])
    monkeypatch.setattr(z, "ensure_tab", Mock(return_value="2"))
    action = Mock(return_value="")
    monkeypatch.setattr(z, "action", action)

    result = z.move_thread_pane({"id": THREAD}, "Markster", "test")

    assert result == "Already in Markster; focused terminal_7."
    assert action.call_args_list == [
        call("test", "go-to-tab-by-id", "2"),
        call("test", "focus-pane-id", "terminal_7"),
    ]


def test_open_new_thread_routes_and_tiles_its_project_tab(monkeypatch):
    from contextlib import nullcontext

    monkeypatch.setattr(z, "operation_lock", nullcontext)
    monkeypatch.setattr(z, "panes", Mock(side_effect=[[], [
        {"id": 8, "tab_id": 12, "is_focused": True},
    ]]))
    monkeypatch.setattr(z, "project_group", lambda _: "Markster")
    ensure_tab = Mock(return_value="12")
    monkeypatch.setattr(z, "ensure_tab", ensure_tab)
    action = Mock(side_effect=["", "terminal_8", "", ""])
    monkeypatch.setattr(z, "action", action)

    assert z.open_thread({"id": THREAD, "cwd": "/work/markster-core"}, "test") == "terminal_8"

    ensure_tab.assert_called_once_with("test", "Markster")
    assert action.call_args_list[0].args == ("test", "go-to-tab-by-id", "12")
    assert action.call_args_list[1].args[:3] == ("test", "new-pane", "--name")
    assert action.call_args_list[2].args == ("test", "next-swap-layout", "--tab-id", "12")
    assert action.call_args_list[3].args == ("test", "focus-pane-id", "terminal_8")


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
    monkeypatch.setattr(z, "setup_workspace_tabs", Mock())
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
