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


OTHER = "22222222-2222-2222-2222-222222222222"
PANE = {"pane_id": "w8:p2", "workspace_id": "w8", "tab_id": "w8:t1",
        "terminal_id": "term-a", "cwd": "/work"}


class Owner:
    """A shared owner holding the given threads; every one of them counts as loaded."""

    def __init__(self, *threads: dict):
        self.threads = {thread["id"]: thread for thread in threads}
        self.unloaded: dict[str, dict] = {}
        self.calls = 0

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def request(self, method, payload):
        self.calls += 1
        if method == "thread/loaded/list":
            return {"data": list(self.threads)}
        assert method == "thread/read"
        known = {**self.unloaded, **self.threads}
        if payload["threadId"] not in known:
            raise SharedServerError("thread not found")
        return {"thread": known[payload["threadId"]]}


def chat(thread_id: str, name: str | None, cwd: str = "/work", status: str = "idle", flags=()):
    return {"id": thread_id, "name": name, "cwd": cwd,
            "status": {"type": status, "activeFlags": list(flags)}}


def test_launcher_marks_the_pane_for_herdr_and_reports_no_state(monkeypatch):
    command = Mock()
    run = Mock(return_value=SimpleNamespace(returncode=7))
    monkeypatch.setattr(h, "command", command)
    monkeypatch.setattr(h.subprocess, "run", run)
    assert h.run_in_pane(["codex-bin"], {"HERDR_PANE_ID": "w8:p2"}) == 7
    assert run.call_args.kwargs["env"]["HERDR_AGENT"] == "codex"
    # Herdr reads working, blocked and idle from the screen; a reported state would override
    # that and go stale when the view moves to another chat.
    command.assert_not_called()


def test_thread_label_and_title_follow_the_default_codex_layout():
    long = "Markster production recovery and NAS backup verification"
    assert h.thread_label({"name": long}) == "Markster production recovery and NAS backup v..."
    assert h.thread_label({"name": "x" * 48}) == "x" * 48
    assert h.thread_label({"id": THREAD, "name": "  "}) is None
    assert h.title_thread("Fix login | markster-core") == "Fix login"
    assert h.title_thread("A | B | repo") == "A | B"
    # An unnamed chat shows only its project, which must never be read as a chat's name.
    assert h.title_thread("markster-core") is None
    assert h.title_thread("") is None


THIRD = "33333333-3333-3333-3333-333333333333"


@pytest.mark.parametrize(("title", "threads", "expected"), [
    ("Deploy | repo", [chat(THREAD, "Deploy"), chat(OTHER, "Review")], (THREAD, True)),
    ("Review | repo", [chat(THREAD, "Deploy"), chat(OTHER, "Review")], (OTHER, True)),
    # Two loaded chats share the name: the earlier identity stands, unproven.
    ("Review | repo", [chat(THREAD, "Review"), chat(OTHER, "Review")], (THREAD, False)),
    # ...and a view that moved onto one of two namesakes cannot be identified at all.
    ("New chat | repo", [chat(THREAD, "Deploy"), chat(OTHER, "New chat"),
                         chat(THIRD, "New chat")], (None, False)),
    # A chat named like the project is not what a one-part title shows.
    ("repo", [chat(THREAD, "Deploy"), chat(OTHER, "repo")], (None, False)),
    ("repo", [chat(THREAD, None), chat(OTHER, "repo")], (THREAD, False)),
    ("", [chat(THREAD, "Deploy")], (THREAD, False)),
    ("Unknown name | repo", [chat(THREAD, "Deploy")], (None, False)),
])
def test_shown_thread_is_certain_only_for_one_loaded_chat_of_that_name(
        title, threads, expected):
    pane = {**PANE, "terminal_title_stripped": title}
    assert h.shown_thread(Owner(*threads), pane, THREAD) == expected


def test_shown_thread_ignores_a_chat_the_owner_no_longer_has_loaded():
    # The view left chat A, which was then unloaded; chat B has the same name. Reading A by
    # ID still works, so only the loaded list can show that the view is on B.
    owner = Owner(chat(OTHER, "Review"))
    owner.unloaded = {THREAD: chat(THREAD, "Review")}
    pane = {**PANE, "terminal_title_stripped": "Review | repo"}
    assert h.shown_thread(owner, pane, THREAD) == (OTHER, True)


def follower(monkeypatch, tmp_path, owner, pane_states):
    """A follower whose Herdr answers with each pane state in turn, then repeats the last."""
    commands = []
    states = iter(pane_states)
    last = {}

    def command(session, *args):
        commands.append(args)
        if args[:2] == ("pane", "get"):
            last.update(pane=next(states, last.get("pane")))
            if last["pane"] is None:
                raise SharedServerError("pane_not_found")
            return {"pane": last["pane"]}
        return {}

    monkeypatch.setattr(h, "command", command)
    monkeypatch.setattr(h, "RpcClient", owner)
    monkeypatch.setattr(h, "socket_path", lambda: "/tmp/test.sock")
    monkeypatch.setattr(h, "state_dir", lambda: tmp_path)
    return h.PaneFollower("work", "w8:p2", THREAD), commands


def session_reports(commands):
    return [args[args.index("--agent-session-id") + 1] for args in commands
            if args[:2] == ("pane", "report-agent-session")]


def test_follower_records_the_shown_chat_and_follows_navigation(monkeypatch, tmp_path):
    owner = Owner(chat(THREAD, "Deploy"), chat(OTHER, "Review"))
    first = {**PANE, "terminal_title_stripped": "Deploy | repo"}
    reported = {**first, "agent_session": {"source": "herdr:codex", "kind": "id",
                                           "value": THREAD}}
    moved = {**reported, "terminal_title_stripped": "Review | repo"}
    follow, commands = follower(monkeypatch, tmp_path, owner, [first, reported, reported, moved])

    follow.step()
    assert session_reports(commands) == [THREAD]
    report = next(args for args in commands if args[1] == "report-agent-session")
    assert report[report.index("--source") + 1] == "herdr:codex"
    assert report[report.index("--") + 1:] == (
        "agentroute", "herdr", "client", "--thread", THREAD, "--session", "work")
    assert list(h.saved_panes("work")) == [{**PANE, "thread": THREAD}]

    calls = owner.calls
    follow.step()
    follow.step()
    assert owner.calls == calls  # Nothing changed, so the shared owner is not asked again.
    assert session_reports(commands) == [THREAD]

    follow.step()
    assert session_reports(commands) == [THREAD, OTHER]
    assert list(h.saved_panes("work")) == [{**PANE, "thread": OTHER}]
    assert not any(args[1] in {"report-agent", "release-agent"} for args in commands)


def test_follower_never_replaces_an_identity_on_an_unproven_match(monkeypatch, tmp_path):
    # The view is on chat B, as an exact report says. Chat A, which it was started with, has
    # the same name. The start command must not be written back over the report.
    owner = Owner(chat(THREAD, "Review"), chat(OTHER, "Review"))
    first = {**PANE, "terminal_title_stripped": "Deploy | repo"}
    exact = {**PANE, "terminal_title_stripped": "Review | repo",
             "agent_session": {"source": "herdr:codex", "kind": "id", "value": OTHER}}
    follow, commands = follower(monkeypatch, tmp_path, owner, [first, exact])
    follow.step()
    assert session_reports(commands) == [THREAD]  # Exact at launch, before the title is read.
    follow.step()
    assert session_reports(commands) == [THREAD]
    assert list(h.saved_panes("work")) == [{**PANE, "thread": THREAD}]


def test_follower_keeps_its_record_when_the_view_moves_out_of_sight(monkeypatch, tmp_path):
    owner = Owner(chat(THREAD, "Deploy"), chat(OTHER, None))
    shown = {**PANE, "terminal_title_stripped": "Deploy | repo"}
    unnamed = {**shown, "terminal_title_stripped": "repo"}
    follow, commands = follower(monkeypatch, tmp_path, owner, [shown, unnamed])
    follow.step()
    follow.step()
    assert session_reports(commands) == [THREAD]
    assert list(h.saved_panes("work")) == [{**PANE, "thread": THREAD}]


def test_follower_finds_a_moved_pane_by_its_terminal(monkeypatch, tmp_path):
    owner = Owner(chat(THREAD, "Deploy"))
    shown = {**PANE, "terminal_title_stripped": "Deploy | repo"}
    moved = {**shown, "pane_id": "w9:p1", "workspace_id": "w9", "tab_id": "w9:t1"}
    follow, commands = follower(monkeypatch, tmp_path, owner, [shown, None])
    monkeypatch.setattr(h, "panes", lambda _: [moved])
    follow.step()
    follow.step()
    assert follow.pane == "w9:p1"
    assert [record["pane_id"] for record in h.saved_panes("work")] == ["w9:p1"]


def test_follower_survives_any_failure_and_stops_when_asked(monkeypatch):
    follow = h.PaneFollower("work", "w8:p2", THREAD)
    stopped = h.threading.Event()
    steps = []

    def step():
        steps.append(len(steps))
        if len(steps) == 1:
            raise KeyError("pane")  # A malformed answer once ended tracking for the session.
        if len(steps) == 3:
            stopped.set()

    monkeypatch.setattr(follow, "step", step)
    monkeypatch.setattr(h, "FOLLOW_SECONDS", 0)
    follow.run(stopped)
    assert steps == [0, 1, 2]


def test_shared_owner_does_not_inherit_a_pane_address(monkeypatch, tmp_path):
    from agentroute import shared_server

    monkeypatch.setattr(shared_server, "agentroute_home", lambda: tmp_path)
    environment = shared_server.server_environment({
        "PATH": "/bin", "HERDR_ENV": "1", "HERDR_PANE_ID": "w2:p1", "HERDR_TAB_ID": "w2:t1",
        "HERDR_WORKSPACE_ID": "w2", "HERDR_SESSION": "agentroute", "HERDR_AGENT": "codex",
        "HERDR_SOCKET_PATH": "/tmp/herdr.sock", "HERDR_BIN_PATH": "/usr/local/bin/herdr"})
    # The owner serves every pane. Hooks it runs must not report to the first launcher's.
    assert {name for name in environment if name.startswith("HERDR_")} == {"HERDR_BIN_PATH"}
    assert environment["PATH"] == "/bin"


def relaunch_setup(monkeypatch, tmp_path, owner, pane, argv, registers=True):
    """Herdr and the owner as `relaunch_pane` sees them; returns the kill and launch mocks."""
    state = {"pane": pane}

    def command(session, *args):
        if args[:2] == ("pane", "get"):
            if state["pane"] is None:
                raise SharedServerError("pane_not_found")
            return {"pane": state["pane"]}
        if args[:2] == ("pane", "process-info"):
            return {"process_info": {"foreground_processes": [
                {"pid": 4242, "name": "codex-bin", "argv": argv},
                {"pid": 4241, "name": "python3.12", "argv": ["python", "agentroute"]}]}}
        raise AssertionError(args)

    monkeypatch.setattr(h, "command", command)
    monkeypatch.setattr(h, "RpcClient", owner)
    monkeypatch.setattr(h, "socket_path", lambda: "/run/owner.sock")
    monkeypatch.setattr(h, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(h, "wait_for_shell", lambda *_, **__: True)
    monkeypatch.setattr(h, "panes", lambda _: [pane])
    monkeypatch.setattr(h, "thread_panes", lambda *_: {THREAD: pane} if registers else {})
    monkeypatch.setattr(h.time, "sleep", lambda _: None)
    kill, launch = Mock(), Mock()
    monkeypatch.setattr(h.os, "kill", kill)
    monkeypatch.setattr(h, "launch_client", launch)
    return kill, launch, state


VIEW = ["codex-bin", "--remote", "unix:///run/owner.sock", "--cd", "/work", "resume", THREAD]
SHOWN = {**PANE, "terminal_title_stripped": "Deploy | repo"}


@pytest.mark.parametrize(("argv", "threads", "force", "expected"), [
    (VIEW, [chat(THREAD, "Deploy")], False, f"relaunched {THREAD}"),
    (VIEW, [chat(THREAD, "Deploy", status="active")], True, f"relaunched {THREAD}"),
    (VIEW, [chat(THREAD, "Deploy", status="active")], False, "skipped: chat is running"),
    (VIEW, [chat(THREAD, "Deploy", status="active", flags=["waitingOnApproval"])], True,
     "skipped: chat is approval"),
    (VIEW, [chat(THREAD, "Deploy", status="active", flags=["waitingOnUserInput"])], True,
     "skipped: chat is input"),
    (VIEW, [chat(THREAD, "Deploy", status="systemError")], True, "skipped: chat is error"),
    # A Codex that owns its chat, or is attached somewhere else, is not ours to stop.
    (["codex-bin", "--no-daemon", "resume", THREAD], [chat(THREAD, "Deploy")], True,
     "no view of this shared owner"),
    (["codex-bin", "--remote", "unix:///elsewhere.sock", "resume", THREAD],
     [chat(THREAD, "Deploy")], True, "no view of this shared owner"),
    # Started bare: nothing says which chat it is, whatever the title resembles.
    (VIEW[:5], [chat(THREAD, "Deploy")], True,
     "skipped: the view was started without a chat ID; pass --thread to name it"),
    # The view has moved to a chat with another name, or to a namesake waiting on approval.
    (VIEW, [chat(THREAD, "Other"), chat(OTHER, "Deploy")], True,
     "skipped: the title does not single out this chat"),
    (VIEW, [chat(THREAD, "Deploy"),
            chat(OTHER, "Deploy", status="active", flags=["waitingOnApproval"])], True,
     "skipped: the title does not single out this chat"),
])
def test_relaunch_stops_a_view_only_when_its_chat_is_proven_and_safe(
        monkeypatch, tmp_path, argv, threads, force, expected):
    kill, launch, _ = relaunch_setup(monkeypatch, tmp_path, Owner(*threads), SHOWN, argv)
    assert h.relaunch_pane("work", "w8:p2", None, force) == expected
    relaunched = expected.startswith("relaunched")
    assert kill.called == launch.called == relaunched
    if relaunched:
        kill.assert_called_once_with(4242, h.signal.SIGTERM)
        launch.assert_called_once_with("work", "w8:p2", THREAD)


def test_relaunch_accepts_an_operator_named_chat_for_a_view_started_bare(monkeypatch, tmp_path):
    kill, launch, _ = relaunch_setup(
        monkeypatch, tmp_path, Owner(chat(THREAD, "Deploy")), SHOWN, VIEW[:5])
    assert h.relaunch_pane("work", "w8:p2", THREAD, False) == f"relaunched {THREAD}"
    assert h.relaunch_pane("work", "w8:p2", OTHER, False) == \
        "skipped: the title does not single out this chat"


def test_relaunch_rechecks_before_stopping_and_reports_a_view_that_does_not_return(
        monkeypatch, tmp_path):
    owner = Owner(chat(THREAD, "Deploy"), chat(OTHER, "Review"))
    kill, launch, state = relaunch_setup(monkeypatch, tmp_path, owner, SHOWN, VIEW)
    checks = iter([None, "switched"])
    original = h.shown_thread

    def switching(client, pane, known):
        if next(checks):
            pane = {**pane, "terminal_title_stripped": "Review | repo"}
        return original(client, pane, known)

    monkeypatch.setattr(h, "shown_thread", switching)
    assert h.relaunch_pane("work", "w8:p2", None, True) == \
        "skipped: the title does not single out this chat"
    kill.assert_not_called()

    monkeypatch.setattr(h, "shown_thread", original)
    kill, launch, state = relaunch_setup(monkeypatch, tmp_path, Owner(chat(THREAD, "Deploy")),
                                         SHOWN, VIEW, registers=False)
    assert h.relaunch_pane("work", "w8:p2", None, False).startswith(
        "error: the new view did not register")
    state["pane"] = None
    assert h.relaunch_pane("work", "w8:p2", None, False) == "error: pane not found"


def test_relaunch_command_refuses_before_stopping_anything_and_fails_loudly(monkeypatch):
    from typer.testing import CliRunner

    from agentroute.cli import app

    relaunched = Mock(return_value="error: the new view did not register; inspect the pane")
    monkeypatch.setattr(h, "relaunch_pane", relaunched)
    monkeypatch.setattr(h, "panes", lambda _: [PANE])
    monkeypatch.setattr(h, "load_config", lambda: SimpleNamespace(
        shared_server=SimpleNamespace(enabled=True)))
    monkeypatch.setattr(h, "ensure_server", Mock(side_effect=SharedServerError(
        "shared server runtime/settings differ from this launch")))
    result = CliRunner().invoke(app, ["herdr", "relaunch"])
    assert result.exit_code != 0 and "differ from this launch" in result.output
    relaunched.assert_not_called()  # The new view could not attach, so no old one is stopped.

    monkeypatch.setattr(h, "ensure_server", Mock())
    result = CliRunner().invoke(app, ["herdr", "relaunch"])
    assert result.exit_code == 1 and "did not register" in result.output
    result = CliRunner().invoke(app, ["herdr", "relaunch", "--thread", THREAD])
    assert result.exit_code != 0 and "--pane" in result.output



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
