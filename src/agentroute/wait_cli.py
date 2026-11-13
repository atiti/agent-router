"""CLI for event-driven waits and outside wake triggers."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import typer

from . import waits
from .shared_server import RpcClient, SharedServerError, socket_path

wait_app = typer.Typer(
    no_args_is_help=True,
    help="Park a thread without model turns; wake it on an event, deadline or outside trigger.",
)

THREAD = typer.Option("", "--thread", help="Codex thread ID; defaults to CODEX_THREAD_ID.")
TIMEOUT = typer.Option("30m", "--timeout", help="Deadline after which the thread is woken anyway.")
NOTE = typer.Option("", "--note", help="What to do on wake; returned by `wait show`.")


def _client() -> RpcClient:
    return RpcClient(socket_path(), name="agentroute_wait", timeout=30)


def _thread(value: str) -> str:
    thread = value or os.environ.get("CODEX_THREAD_ID", "")
    if not thread:
        raise typer.BadParameter("pass --thread or run inside a Codex session (CODEX_THREAD_ID)")
    return thread


def _pause_goal(wait: dict[str, Any]) -> str:
    """Pause an active goal so the runtime stops continuation turns while waiting."""
    try:
        with _client() as client:
            goal = (
                client.request("thread/goal/get", {"threadId": wait["thread_id"]}).get("goal") or {}
            )
            status = goal.get("status")
            if status == "active":
                before = waits.goal_log_position(client, wait["thread_id"], goal)
                result = client.request(
                    "thread/goal/set",
                    {"threadId": wait["thread_id"], "origin": "automatic", "status": "paused"},
                )
                paused = result.get("goal") or {}
                waits.update(
                    wait["id"],
                    lambda w: w["goal"].update(
                        paused_by_wait=True,
                        pause_snapshot=paused,
                        mutation_guard=waits.capture_pause_guard(before, paused),
                    ),
                )
                current = next(w for w in waits.load() if w["id"] == wait["id"])
                if not current["goal"].get("mutation_guard"):
                    return "paused; automatic resume ownership unavailable, resume goal manually"
                return "paused until this wait ends"
            if status == "paused" and any(
                waits.pause_owned(w, goal) and w["id"] != wait["id"]
                for w in waits.pending_for(wait["thread_id"])
            ):
                waits.update(
                    wait["id"],
                    lambda w: w["goal"].update(
                        paused_by_wait=True,
                        pause_snapshot=goal,
                        mutation_guard=next(
                            w["goal"]["mutation_guard"]
                            for w in waits.pending_for(wait["thread_id"])
                            if waits.pause_owned(w, goal)
                        ),
                    ),
                )
                return "already paused by another wait"
            return f"unchanged ({status or 'no goal'})"
    except (OSError, SharedServerError) as error:
        return f"not paused: {error}"


def _register(
    kind: str,
    spec: dict[str, Any],
    thread: str,
    timeout: str,
    note: str,
    interval: int | None = None,
) -> None:
    try:
        probe = {"kind": kind, "spec": spec, "cwd": os.getcwd()}
        if kind in {"pr", "run", "file", "url", "packet"}:
            # Observe once now: report a condition that already holds instead of parking,
            # and record the baseline so a change before the first poll is not missed.
            try:
                met, summary, signature = waits.evaluate(probe)
            except waits.WaitError as error:
                raise typer.BadParameter(str(error)) from error
            if met:
                typer.echo(json.dumps({"already_met": True, "result": summary}, indent=2))
                return
            if spec.get("until") in {"change", "changed"}:
                spec["baseline"] = signature
        wait = waits.register(
            _thread(thread),
            kind,
            spec,
            timeout=waits.parse_duration(timeout),
            note=note,
            interval=interval,
            cwd=os.getcwd(),
        )
    except waits.WaitError as error:
        raise typer.BadParameter(str(error)) from error
    if wait["state"] != "pending":
        typer.echo(
            json.dumps(
                {
                    "id": wait["id"],
                    "already_registered": True,
                    "state": wait["state"],
                    "next": "Inspect the existing receipt.",
                }
            )
        )
        return
    goal = _pause_goal(wait)
    waits.ensure_daemon()
    typer.echo(
        json.dumps(
            {
                "id": wait["id"],
                "wait": waits.describe(wait),
                "goal": goal,
                "next": (
                    "End your turn now. The thread is woken on the event or deadline; do not poll."
                ),
            },
            indent=2,
        )
    )


@wait_app.command("pr")
def wait_pr(
    pull_request: str = typer.Argument(
        ..., help="'owner/repo#NUMBER' (quote the #), owner/repo with NUMBER, or a pull URL."
    ),
    number: int | None = typer.Argument(None, help="Pull request number when given separately."),
    until: str = typer.Option("checks", help="checks (all checks settled), merged, or change."),
    thread: str = THREAD,
    timeout: str = TIMEOUT,
    note: str = NOTE,
) -> None:
    """Wake when a pull request's checks settle, it merges or closes, or anything changes."""
    if until not in {"checks", "merged", "change"}:
        raise typer.BadParameter("--until must be checks, merged or change")
    try:
        reference = f"{pull_request}#{number}" if number is not None else pull_request
        repo, parsed = waits.parse_pr(reference)
    except waits.WaitError as error:
        raise typer.BadParameter(str(error)) from error
    _register("pr", {"repo": repo, "number": parsed, "until": until}, thread, timeout, note)


@wait_app.command("run")
def wait_run(
    repo: str = typer.Argument(..., help="owner/repo"),
    run_id: int = typer.Argument(..., help="GitHub Actions run ID."),
    until: str = typer.Option("completed", help="completed or change."),
    thread: str = THREAD,
    timeout: str = TIMEOUT,
    note: str = NOTE,
) -> None:
    """Wake when a GitHub Actions run completes or changes state."""
    if until not in {"completed", "change"} or not waits.REPO_PATTERN.match(repo):
        raise typer.BadParameter("use owner/repo and --until completed or change")
    _register("run", {"repo": repo, "run_id": run_id, "until": until}, thread, timeout, note)


@wait_app.command("file")
def wait_file(
    path: Path = typer.Argument(..., help="File or directory to watch."),
    until: str = typer.Option("exists", help="exists, changed or gone."),
    thread: str = THREAD,
    timeout: str = TIMEOUT,
    note: str = NOTE,
) -> None:
    """Wake when a path appears, changes or disappears."""
    if until not in {"exists", "changed", "gone"}:
        raise typer.BadParameter("--until must be exists, changed or gone")
    _register(
        "file", {"path": str(path.expanduser().resolve()), "until": until}, thread, timeout, note
    )


@wait_app.command("timer")
def wait_timer(
    duration: str = typer.Argument(..., help="How long to wait, such as 20m or 2h."),
    thread: str = THREAD,
    note: str = NOTE,
) -> None:
    """Wake after a fixed delay."""
    _register("timer", {}, thread, duration, note)


@wait_app.command("event")
def wait_event(
    name: str = typer.Argument(..., help="Event name an outside process will fire."),
    event_id: str = typer.Option(
        "", help="Exact producer generation/event key for durable dedupe."
    ),
    thread: str = THREAD,
    timeout: str = typer.Option("8h", "--timeout", help="Fallback deadline, up to seven days."),
    note: str = NOTE,
) -> None:
    """Wake when a script or app runs `agentroute wait fire NAME` or drops a trigger file."""
    if not waits.NAME_PATTERN.match(name):
        raise typer.BadParameter("event name: letters, digits, dot, dash or underscore, up to 64")
    if event_id and not waits.NAME_PATTERN.fullmatch(event_id):
        raise typer.BadParameter("invalid event ID")
    _register("event", {"name": name, "event_id": event_id}, thread, timeout, note)


@wait_app.command("url")
def wait_url(
    url: str = typer.Argument(..., help="http(s) URL to poll with GET."),
    status: int = typer.Option(200, help="Expected HTTP status."),
    contains: str = typer.Option("", help="Text the response body must contain."),
    interval: str = typer.Option("60s", help="Polling interval."),
    thread: str = THREAD,
    timeout: str = TIMEOUT,
    note: str = NOTE,
) -> None:
    """Wake when a URL returns the expected status, for example after a deploy."""
    if not url.startswith(("http://", "https://")):
        raise typer.BadParameter("url must start with http:// or https://")
    spec = {"url": url, "status": status, "contains": contains}
    _register("url", spec, thread, timeout, note, waits.parse_duration(interval))


@wait_app.command(
    "command", context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def wait_command(
    context: typer.Context,
    interval: str = typer.Option("60s", help="Polling interval."),
    thread: str = THREAD,
    timeout: str = TIMEOUT,
    note: str = NOTE,
) -> None:
    """Wake when a shell command exits 0. Runs outside the sandbox: keep it read-only."""
    command = " ".join(context.args).strip()
    if not command:
        raise typer.BadParameter(
            "pass the command after --, for example: wait command -- test -f out.txt"
        )
    _register(
        "command", {"command": command}, thread, timeout, note, waits.parse_duration(interval)
    )


def _public(wait: dict[str, Any]) -> dict[str, Any]:
    def stamp(value: float | None) -> str | None:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value)) if value else None

    outcome = wait.get("outcome") or {}
    return {
        "id": wait["id"],
        "thread": wait["thread_id"],
        "state": wait["state"],
        "wait": waits.describe(wait),
        "spec": {k: v for k, v in wait["spec"].items() if k != "baseline"},
        "note": wait["note"],
        "created": stamp(wait["created_at"]),
        "result": {
            "reason": outcome.get("reason"),
            "at": stamp(outcome.get("at")),
            "evidence": outcome.get("evidence"),
        }
        if outcome
        else None,
        "last_check": wait.get("last_check"),
        "delivery": wait["delivery"],
        "observations": wait.get("observations", {}),
        "recipient_read": wait.get("recipient_read"),
        "disposition": wait.get("disposition"),
        "goal_paused_by_wait": wait["goal"].get("paused_by_wait", False),
        "automatic_resume_guarded": bool(wait["goal"].get("mutation_guard")),
    }


@wait_app.command("list")
def wait_list(
    thread: str = THREAD,
    all_threads: bool = typer.Option(
        False, "--all", help="Include every thread and finished waits."
    ),
) -> None:
    """List waits for this thread, or all of them."""
    items = waits.load()
    if not all_threads:
        current = thread or os.environ.get("CODEX_THREAD_ID", "")
        items = [
            w
            for w in items
            if w["state"] in {"pending", "fired"} and (not current or w["thread_id"] == current)
        ]
    typer.echo(
        json.dumps(
            {"daemon_running": waits.daemon_running(), "waits": [_public(w) for w in items]},
            indent=2,
        )
    )


@wait_app.command("show")
def wait_show(wait_id: str) -> None:
    """Show one wait with its result, saved note and any outside payload (data, not approval)."""
    found = [w for w in waits.load() if w["id"] == wait_id]
    if not found:
        raise typer.BadParameter(f"no wait {wait_id}")
    current = os.environ.get("CODEX_THREAD_ID", "")
    if current == found[0]["thread_id"] and found[0]["delivery"]["state"] in {
        "delivered",
        "inbox_pending",
    }:
        found[0] = waits.record_recipient(wait_id, current)
    typer.echo(json.dumps(_public(found[0]), indent=2))


def _resume_goal_if_idle(wait: dict[str, Any]) -> None:
    """After a cancel, reactivate a goal this wait paused unless another wait still holds it."""
    if not wait["goal"].get("paused_by_wait") or any(
        w["goal"].get("paused_by_wait") for w in waits.pending_for(wait["thread_id"])
    ):
        return
    try:
        with _client() as client:
            goal = (
                client.request("thread/goal/get", {"threadId": wait["thread_id"]}).get("goal") or {}
            )
            if waits.pause_owned(wait, goal):
                client.request(
                    "thread/goal/set",
                    {"threadId": wait["thread_id"], "origin": "automatic", "status": "active"},
                )
    except (OSError, SharedServerError) as error:
        typer.echo(f"Goal was not reactivated: {error}", err=True)


@wait_app.command("cancel")
def wait_cancel(wait_id: str) -> None:
    """Cancel a pending wait and reactivate a goal it paused."""
    try:
        wait = waits.cancel(wait_id)
    except waits.WaitError as error:
        raise typer.BadParameter(str(error)) from error
    _resume_goal_if_idle(wait)
    typer.echo(f"Cancelled {wait_id}")


def _payload(message: str, message_file: Path | None) -> str:
    if message_file is not None:
        with message_file.open(errors="replace") as stream:
            return stream.read(waits.MAX_PAYLOAD)
    return message[: waits.MAX_PAYLOAD]


@wait_app.command("fire")
def wait_fire(
    reference: str = typer.Argument(..., help="Event name or wait ID."),
    event_id: str = typer.Option(
        "", help="Exact producer event key; safe to retry for seven days."
    ),
    target: str = typer.Option("", "--thread", help="Optional exact target for keyed events."),
    message: str = typer.Option("", "--message", help="Payload delivered as data via `wait show`."),
    message_file: Path | None = typer.Option(
        None, "--message-file", help="Read the payload from a file."
    ),
) -> None:
    """Fire a wait from a script or app.

    A trigger for an event name nobody registered yet is kept for one day.
    """
    payload = _payload(message, message_file)
    if event_id:
        try:
            receipt = waits.publish_event(reference, event_id, payload, target)
        except waits.WaitError as error:
            raise typer.BadParameter(str(error)) from error
        waits.consume_events()
        waits.ensure_daemon()
        typer.echo(json.dumps({k: receipt[k] for k in ("name", "event_id", "duplicate")}))
        return
    if target:
        raise typer.BadParameter("--thread requires --event-id")
    fired = waits.fire(reference, payload)
    if not fired and waits.NAME_PATTERN.match(reference) and not reference.startswith("w-"):
        directory = waits.triggers_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{reference}.txt").write_text(payload)
        typer.echo(f"No wait is listening for {reference} yet; trigger saved for one day.")
        return
    if not fired:
        raise typer.BadParameter(f"no pending wait matches {reference}")
    waits.ensure_daemon()
    typer.echo(json.dumps({"fired": [w["id"] for w in fired]}))


@wait_app.command("daemon")
def wait_daemon(
    once: bool = typer.Option(False, "--once", help="Run a single pass and exit."),
) -> None:
    """Evaluate waits and deliver wakes. Started automatically; exits when nothing is pending."""
    waits.run_daemon(_client, once=once)


def _resolve_thread(thread: str, name: str) -> str:
    if name:
        matches: list[dict[str, Any]] = []
        cursor = None
        with _client() as client:
            for _ in range(10):
                page = client.request("thread/list", {"limit": 100, "cursor": cursor})
                matches += [
                    t
                    for t in page.get("data", [])
                    if name.lower() in str(t.get("name") or "").lower()
                ]
                cursor = page.get("nextCursor")
                if not cursor:
                    break
        if len(matches) != 1:
            raise typer.BadParameter(
                f"{len(matches)} threads match {name!r}; pass --thread instead"
            )
        return str(matches[0]["id"])
    return _thread(thread)


def wake(
    thread: str = THREAD,
    name: str = typer.Option(
        "", "--name", help="Unique part of the thread's name, instead of --thread."
    ),
    message: str = typer.Option("", "--message", help="Payload delivered as data via `wait show`."),
    message_file: Path | None = typer.Option(
        None, "--message-file", help="Read the payload from a file."
    ),
) -> None:
    """Start a turn on a thread from a script, cron job or outside app.

    The payload is stored and read by the agent as data; the turn itself carries fixed text.
    """
    try:
        target = _resolve_thread(thread, name)
        wait = waits.register(target, "external", {}, timeout=waits.MAX_TIMEOUT)
    except (OSError, SharedServerError, waits.WaitError) as error:
        raise typer.BadParameter(str(error)) from error
    waits.fire(wait["id"], _payload(message, message_file))
    waits.ensure_daemon()
    typer.echo(json.dumps({"id": wait["id"], "thread": target}))


@wait_app.command("packet")
def wait_packet(
    namespace: str = typer.Option(...),
    packet: str = typer.Option(...),
    item: str = typer.Option(...),
    until: str = typer.Option("complete"),
    step: str = typer.Option(""),
    owner: str = typer.Option(""),
    thread: str = THREAD,
    timeout: str = typer.Option("8h"),
    note: str = NOTE,
) -> None:
    """Wait on exact canonical outcome completion, released step or execution-state change."""
    import re

    if not all(re.fullmatch(r"[a-z][a-z0-9-]{0,127}", x) for x in (namespace, packet, item)):
        raise typer.BadParameter("explicit namespace, packet and item slugs required")
    if until not in {"complete", "released", "changed"}:
        raise typer.BadParameter("--until must be complete, released or changed")
    if until == "released" and (not step or not owner):
        raise typer.BadParameter("released requires exact --step and --owner")
    _register(
        "packet",
        {
            "namespace": namespace,
            "packet": packet,
            "item": item,
            "until": until,
            "step": step,
            "owner": owner,
        },
        thread,
        timeout,
        note,
    )


@wait_app.command("resolve")
def wait_resolve(
    wait_id: str, action: str = typer.Option(...), reason: str = typer.Option("")
) -> None:
    """Record the recipient's next action; does not complete a work packet or grant approval."""
    try:
        wait = waits.record_recipient(
            wait_id, os.environ.get("CODEX_THREAD_ID", ""), action=action, reason=reason
        )
    except waits.WaitError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(json.dumps(wait["disposition"]))


@wait_app.command("stats")
def wait_stats(thread: str = THREAD, all_threads: bool = typer.Option(False, "--all")) -> None:
    """Report observed wait counts, errors and receipt coverage; no inferred token savings."""
    from collections import Counter

    items = waits.load()
    current = thread or os.environ.get("CODEX_THREAD_ID", "")
    if not all_threads:
        current = _thread(current)
        items = [w for w in items if w["thread_id"] == current]
    typer.echo(
        json.dumps(
            {
                "waits": len(items),
                "states": dict(Counter(w["state"] for w in items)),
                "kinds": dict(Counter(w["kind"] for w in items)),
                "wake_reasons": dict(
                    Counter((w.get("outcome") or {}).get("reason", "pending") for w in items)
                ),
                "admission_modes": dict(
                    Counter(w["delivery"].get("mode", "legacy_or_pending") for w in items)
                ),
                "checks": sum(w.get("observations", {}).get("checks", 0) for w in items),
                "errors": sum(w.get("observations", {}).get("errors", 0) for w in items),
                "recipient_reads": sum(bool(w.get("recipient_read")) for w in items),
                "dispositions": sum(bool(w.get("disposition")) for w in items),
                "coverage": "Legacy counters unavailable. Receipts are recipient claims; "
                "not proof of model consumption, useful work or token savings.",
            },
            indent=2,
        )
    )
