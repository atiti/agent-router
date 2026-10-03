"""Explicit work identities persisted in Codex's native thread attachments."""

from __future__ import annotations

import json
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import typer

from .shared_server import RpcClient, SharedServerError, socket_path

WORK_TYPE = "agentroute.work.v1"
work_app = typer.Typer(no_args_is_help=True, help="Link chats to a shared work item.")
sessions_app = typer.Typer(no_args_is_help=True, help="Find explicitly related Codex chats.")


def identity(value: str) -> str:
    """Use a stable URL or explicit local UUID; never infer links from prompt similarity."""
    if value.startswith("local:"):
        return f"local:{UUID(value[6:])}"
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("work ID must be an HTTPS issue/PR URL or local:<UUID>")
    if parsed.query or parsed.fragment:
        raise ValueError("use the canonical work URL without a query or fragment")
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


def pages(client: RpcClient, method: str, params: dict) -> Iterator[dict]:
    cursor = None
    while True:
        page = client.request(method, {**params, "cursor": cursor, "limit": 100})
        yield from page["data"]
        next_cursor = page.get("nextCursor")
        if not next_cursor:
            return
        if next_cursor == cursor:
            raise SharedServerError("server returned a repeated pagination cursor")
        cursor = next_cursor


def owners(client: RpcClient, work: str) -> list[dict]:
    return list(
        pages(
            client,
            "thread/attachmentOwner/list",
            {
                "attachmentType": WORK_TYPE,
                "identityKey": identity(work),
                "archived": None,
            },
        )
    )


@work_app.command("new")
def new() -> None:
    """Generate a work ID when no ticket exists yet."""
    typer.echo(f"local:{uuid4()}")


@work_app.command("link")
def link(work: str, thread: str = typer.Option(...), label: str = typer.Option("")) -> None:
    """Attach an explicit work ID to a chat; repeat for each related chat."""
    try:
        work = identity(work)
        if len(label) > 200:
            raise ValueError("label must be at most 200 characters")
        with RpcClient(socket_path()) as client:
            result = client.request(
                "thread/attachment/add",
                {
                    "threadId": thread,
                    "attachmentType": WORK_TYPE,
                    "identityKey": work,
                    "payload": {"version": 1, "label": label},
                },
            )
        typer.echo(json.dumps(result, indent=2))
    except (OSError, SharedServerError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error


@work_app.command("unlink")
def unlink(work: str, thread: str = typer.Option(...)) -> None:
    """Remove one work link while preserving the chat."""
    try:
        with RpcClient(socket_path()) as client:
            client.request(
                "thread/attachment/remove",
                {
                    "threadId": thread,
                    "attachmentType": WORK_TYPE,
                    "identityKey": identity(work),
                },
            )
        typer.echo("Work link removed.")
    except (OSError, SharedServerError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error


@work_app.command("show")
def show(work: str) -> None:
    """List all linked chats, including archived chats, in this Codex home."""
    try:
        with RpcClient(socket_path()) as client:
            result = {"work": identity(work), "sessions": owners(client, work)}
        typer.echo(json.dumps(result, indent=2))
    except (OSError, SharedServerError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error


@sessions_app.command("related")
def related(thread: str = typer.Option(...)) -> None:
    """Find chats that share a native work attachment with the specified chat."""
    try:
        with RpcClient(socket_path()) as client:
            attachments = pages(client, "thread/attachment/list", {"threadId": thread})
            links = [a for a in attachments if a["attachmentType"] == WORK_TYPE]
            result = [
                {"work": a["identityKey"], "sessions": owners(client, a["identityKey"])}
                for a in links
            ]
        typer.echo(json.dumps({"thread": thread, "work": result}, indent=2))
    except (OSError, SharedServerError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
