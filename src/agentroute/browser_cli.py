"""Operator commands for private alias bindings to existing browser profiles."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import typer

from . import browser_profiles as bp

browser_app = typer.Typer(
    no_args_is_help=True,
    help="Bind aliases to existing browser profiles for the bundled browser plugin.",
)

USER_DATA = typer.Option(
    None, "--user-data-dir", help="Browser user-data root; defaults to the Chrome root."
)
JSON_OUT = typer.Option(False, "--json", help="Print machine-readable JSON.")


def _fail(error: Exception) -> None:
    typer.echo(f"error: {error}", err=True)
    raise typer.Exit(1)


def _print_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True))


@browser_app.command("profiles")
def profiles(
    user_data_dir: Path | None = USER_DATA,
    as_json: bool = JSON_OUT,
    include_email: bool = typer.Option(
        False, "--include-email", help="Include each profile's signed-in email (JSON only)."
    ),
) -> None:
    """List existing browser profile directories and their display labels."""
    if include_email and not as_json:
        raise typer.BadParameter("--include-email requires --json")
    try:
        found = bp.list_profiles(user_data_dir, include_email=include_email)
    except bp.BrowserProfileError as error:
        _fail(error)
    if as_json:
        rows = []
        for profile in found:
            row: dict[str, Any] = {"profile_dir": profile.profile_dir, "label": profile.label}
            if include_email:
                row["email"] = profile.email
            rows.append(row)
        _print_json(rows)
        return
    if not found:
        typer.echo("No browser profiles found.")
    for profile in found:
        typer.echo(f"{profile.profile_dir}\t{profile.label}")


@browser_app.command("bind")
def bind(
    alias: str,
    profile_dir: str = typer.Option(..., "--profile-dir", help="Existing profile directory."),
    user_data_dir: Path | None = USER_DATA,
    site: list[str] = typer.Option([], "--site", help="Site label for selection; repeatable."),
    workflow: list[str] = typer.Option(
        [], "--workflow", help="Workflow label for selection; repeatable."
    ),
    mode: str | None = typer.Option(
        None, "--mode", help="off, browser, computer or both; new bindings default to browser."
    ),
) -> None:
    """Bind ALIAS to one existing profile directory in the private sidecar."""
    try:
        binding = bp.bind(
            alias, profile_dir, user_data_dir=user_data_dir, sites=site,
            workflows=workflow, mode=mode,
        )
    except bp.BrowserProfileError as error:
        _fail(error)
    typer.echo(f"Bound {binding.alias} to {binding.profile_dir} (mode: {binding.mode}).")


@browser_app.command("unbind")
def unbind(alias: str) -> None:
    """Remove one alias from the private sidecar."""
    try:
        bp.unbind(alias)
    except bp.BrowserProfileError as error:
        _fail(error)
    typer.echo(f"Removed {alias}.")


@browser_app.command("mode")
def mode(
    alias: str,
    value: str = typer.Argument(..., metavar="off|browser|computer|both"),
) -> None:
    """Set which kind of automated use ALIAS permits."""
    try:
        binding = bp.set_mode(alias, value)
    except bp.BrowserProfileError as error:
        _fail(error)
    typer.echo(f"{binding.alias}: mode {binding.mode}.")


@browser_app.command("bindings")
def bindings(as_json: bool = JSON_OUT) -> None:
    """List aliases with their profile, mode, labels and whether the profile still exists."""
    try:
        current = bp.load_bindings()
    except bp.BrowserProfileError as error:
        _fail(error)
    rows = []
    for alias in sorted(current):
        binding = current[alias]
        try:
            bp.resolve_binding(alias)
            state = "ok"
        except bp.BrowserProfileError:
            state = "missing profile"
        rows.append({**binding.to_json(), "alias": alias, "state": state})
    if as_json:
        _print_json(rows)
        return
    if not rows:
        typer.echo(f"No browser bindings in {bp.sidecar_path()}.")
    for row in rows:
        labels = ", ".join(
            [f"site:{s}" for s in row.get("sites", [])]
            + [f"workflow:{w}" for w in row.get("workflows", [])]
        )
        typer.echo(
            f"{row['alias']}\t{row['profile_dir']}\t{row['mode']}\t{row['state']}"
            + (f"\t{labels}" if labels else "")
        )


@browser_app.command("resolve")
def resolve(
    alias: str | None = typer.Argument(None),
    site: str | None = typer.Option(None, "--site", help="Select the one binding for a site."),
    workflow: str | None = typer.Option(
        None, "--workflow", help="Select the one binding for a workflow."
    ),
    as_json: bool = JSON_OUT,
    env: bool = typer.Option(
        False, "--env", help="Print shell exports for the browser plugin; requires browser mode."
    ),
    check_extension: bool = typer.Option(
        True, "--check-extension/--no-check-extension",
        help="Ask the browser plugin's checker whether its extension is enabled.",
    ),
) -> None:
    """Resolve one binding to its exact current profile and launch settings."""
    try:
        resolved = bp.resolve_binding(alias, site=site, workflow=workflow)
    except bp.BrowserProfileError as error:
        _fail(error)
    if env:
        if not resolved.browser_allowed:
            typer.echo(
                f"error: {resolved.alias} has mode {resolved.mode}; browser use is not allowed",
                err=True,
            )
            raise typer.Exit(2)
        for key, value in sorted(resolved.env.items()):
            typer.echo(f"export {key}={shlex.quote(value)}")
        return
    data = resolved.to_json()
    if check_extension:
        data["extension"] = bp.extension_readiness(resolved)
    if as_json:
        _print_json(data)
        return
    typer.echo(f"alias: {data['alias']}")
    typer.echo(f"mode: {data['mode']}")
    typer.echo(f"profile: {data['profile_dir']}")
    typer.echo(f"user data: {data['user_data_dir']}")
    typer.echo(f"launch: {' '.join(shlex.quote(arg) for arg in data['launch_args'])}")
    for key, value in sorted(data["env"].items()):
        typer.echo(f"env: {key}={shlex.quote(value)}")
    if "extension" in data:
        extension = data["extension"]
        detail = f" ({extension['detail']})" if extension.get("detail") else ""
        typer.echo(f"extension: {extension['status']}{detail}")
    typer.echo("authentication: not checked")


@browser_app.command("open")
def open_profile(
    alias: str,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Verify the launch without opening Chrome."
    ),
    as_json: bool = JSON_OUT,
) -> None:
    """Open about:blank in ALIAS's Chrome profile through the bundled vendor opener."""
    try:
        result = bp.open_profile(alias, dry_run=dry_run)
    except bp.BrowserProfileError as error:
        _fail(error)
    if as_json:
        _print_json(result)
        return
    status = "Verified dry run" if dry_run else "Requested Chrome window"
    typer.echo(f"{status}: {alias} ({result['profile_dir']}, mode {result['mode']}).")
    typer.echo("authentication: not checked; terminal connection: not established")
