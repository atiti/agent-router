"""Routine approval rules: let low-risk, read-only commands skip approval review.

Codex reviews every command that must leave the sandbox unless a ``prefix_rule`` allows it.
Most such requests are routine reads (pull request status, the work tracker, AgentRoute's own
state), so each one costs a review round trip without changing the outcome. This module writes
a marked, idempotent block of allow rules into ``$CODEX_HOME/rules/default.rules`` and explains
why a given command line would still be reviewed.

Mutations, credential reads and anything that executes caller-supplied code stay reviewed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import typer

from .config import agentroute_home, codex_home

BEGIN = "# >>> agentroute routine approvals >>>"
END = "# <<< agentroute routine approvals <<<"
EXTRA = "# extra: "

GH_READ = [
    ["pr", "view"], ["pr", "checks"], ["pr", "list"], ["pr", "diff"], ["pr", "status"],
    ["run", "view"], ["run", "list"], ["issue", "view"], ["issue", "list"], ["issue", "status"],
    ["repo", "view"], ["release", "view"], ["release", "list"], ["workflow", "view"],
    ["workflow", "list"], ["search"], ["auth", "status"],
]  # fmt: skip
AGENTROUTE_READ = [
    ["history"], ["why"], ["version"], ["analytics"], ["audit-report"], ["stats"], ["models"],
    ["classifier-status"], ["gh-get"], ["packet-read"], ["approvals", "status"],
    ["approvals", "explain"],
    ["wait", "pr"], ["wait", "run"], ["wait", "file"], ["wait", "timer"], ["wait", "event"],
    ["wait", "list"], ["wait", "show"], ["wait", "cancel"], ["wait", "packet"],
    ["wait", "stats"], ["wait", "resolve"], ["herdr", "list"],
]  # fmt: skip
# Team coordination stays inside the local ledger; launching or cleaning up workers is excluded.
TEAM = ["status", "messages", "inbox", "inbox-ack", "ack", "send"]
# Pure filters with no option that writes a file or executes a program.
FILTERS = ["head", "tail", "wc", "cut", "jq", "grep"]
INTERPRETERS = {
    "python",
    "python3",
    "bash",
    "sh",
    "zsh",
    "node",
    "ruby",
    "perl",
    "env",
    "sudo",
    "uv",
}
UNSAFE_WORD = re.compile(r"[{}*?\[\]\\~^#$`<>|&;()\s]")


def rules_path() -> Path:
    return codex_home() / "rules" / "default.rules"


def routine_patterns() -> list[tuple[list[str], str]]:
    python = str(agentroute_home() / "venv" / "bin" / "python")
    patterns: list[tuple[list[str], str]] = []
    patterns += [(["gh", *verb], "read-only GitHub query") for verb in GH_READ]
    patterns += [
        (["agentroute", *verb], "read-only or wait-registration AgentRoute command")
        for verb in AGENTROUTE_READ
    ]
    for command in TEAM:
        patterns.append((["agentroute", "herdr", "team", command], "local team ledger"))
        patterns.append(
            ([python, "-m", "agentroute.cli", "herdr", "team", command], "local team ledger")
        )
    patterns += [([name], "read-only output filter") for name in FILTERS]
    return patterns


def validate_extra(tokens: list[str]) -> list[str]:
    """Accept only a concrete tool prefix; never a bare interpreter or shell syntax."""
    if not tokens or any(not token or UNSAFE_WORD.search(token) for token in tokens):
        raise ValueError("extra rule must be plain words without shell syntax, ~ or wildcards")
    name = Path(tokens[0]).name
    if name in INTERPRETERS:
        script = tokens[1] if len(tokens) > 1 else ""
        if not script.startswith("/") or script.startswith("-") or not Path(script).is_file():
            raise ValueError(
                f"{name} needs an existing absolute script path as its second word; a bare "
                "interpreter would allow arbitrary code"
            )
    if name in {"rm", "curl", "wget", "ssh", "scp", "dd", "chmod", "chown"}:
        raise ValueError(f"{name} is not a routine read-only command")
    return tokens


def existing_extras(text: str) -> list[list[str]]:
    extras: list[list[str]] = []
    if BEGIN in text and END in text:
        for line in text.split(BEGIN, 1)[1].split(END, 1)[0].splitlines():
            if line.startswith(EXTRA):
                extras.append(json.loads(line[len(EXTRA) :]))
    return extras


def legacy_tracker_prefix(tokens: list[str]) -> bool:
    return (len(tokens) >= 2 and Path(tokens[0]).name in {"python", "python3"}
            and Path(tokens[1]).name == "work_packets.py")


def render(extras: list[list[str]]) -> str:
    lines = [
        BEGIN,
        "# Managed by `agentroute approvals install`; edits inside this block are overwritten.",
        "# These commands run outside the sandbox without an approval review.",
    ]
    seen: set[str] = set()
    for pattern, why in [
        *routine_patterns(),
        *[(extra, "user-added routine command") for extra in extras],
    ]:
        key = json.dumps(pattern)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f'prefix_rule(pattern={key}, decision="allow", justification="{why}")')
    lines += [EXTRA + json.dumps(extra) for extra in extras]
    lines.append(END)
    return "\n".join(lines) + "\n"


def install(extras: list[list[str]] | None = None, path: Path | None = None) -> tuple[Path, int]:
    path = path or rules_path()
    text = path.read_text() if path.exists() else ""
    # Old blanket tracker prefix also allowed claim/checkpoint/sync. Migrate only
    # this known managed extra to the dedicated read-only command.
    merged = [extra for extra in existing_extras(text) if not legacy_tracker_prefix(extra)]
    for extra in extras or []:
        if legacy_tracker_prefix(extra):
            raise ValueError("work_packets.py mutates state; use agentroute packet-read")
        if validate_extra(extra) not in merged:
            merged.append(extra)
    block = render(merged)
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        text = head + block + rest.split(END, 1)[1].lstrip("\n")
    else:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".rules.tmp")
    temporary.write_text(text)
    temporary.replace(path)
    return path, block.count("prefix_rule(")


def uninstall(path: Path | None = None) -> bool:
    path = path or rules_path()
    text = path.read_text() if path.exists() else ""
    if BEGIN not in text or END not in text:
        return False
    head, rest = text.split(BEGIN, 1)
    path.write_text(head.rstrip("\n") + "\n" + rest.split(END, 1)[1].lstrip("\n"))
    return True


def allowed_patterns(path: Path | None = None) -> list[list[str]]:
    """Every literal allow pattern in the rules file, managed or not."""
    path = path or rules_path()
    text = path.read_text() if path.exists() else ""
    found = re.findall(r'pattern\s*=\s*(\[[^\]]*\])\s*,\s*decision\s*=\s*"allow"', text)
    patterns = []
    for item in found:
        try:
            patterns.append(json.loads(item))
        except ValueError:
            continue
    return patterns


def explain(command: str, path: Path | None = None) -> dict[str, object]:
    """Say whether a command line can match allow rules, and how to rephrase it if not.

    This mirrors Codex's plain-command parser conservatively: a script is only matched against
    rules when every word is literal and segments are joined by pipes, ``&&``, ``||`` or ``;``.
    """
    problems: list[str] = []
    quoted = re.sub(r"'[^']*'", "Q", command)
    bare = re.sub(r'"[^"]*"', "Q", quoted)
    if "<" in bare or ">" in bare:
        problems.append("redirection (such as 2>&1 or > file) disables rule matching; remove it")
    if "$" in quoted or "`" in quoted:
        problems.append(
            "variables and command substitution disable rule matching; inline the values"
        )
    if "~" in bare:
        problems.append("an unquoted ~ disables rule matching; write the absolute path")
    if re.search(r"[*?\[\]{}#^\\]", bare):
        problems.append(
            "unquoted * ? [ ] { } # ^ or \\ disables rule matching; single-quote that argument"
        )
    if re.search(r"(^|[;&|]\s*)[A-Za-z_][A-Za-z0-9_]*=\S", bare):
        problems.append(
            "an environment assignment prefix disables rule matching; drop it or use a wrapper"
        )
    if "(" in bare or ")" in bare:
        problems.append("subshells disable rule matching")
    segments = [s.strip() for s in re.split(r"\|\||&&|[|;]", bare) if s.strip()]
    patterns = allowed_patterns(path)
    unmatched = []
    for segment in segments:
        words = segment.split()
        if not any(words[: len(pattern)] == pattern for pattern in patterns):
            unmatched.append(segment)
    if unmatched and not problems:
        problems.append("no allow rule covers: " + "; ".join(unmatched))
    return {"runs_without_review": not problems, "problems": problems, "segments": segments}


approvals_app = typer.Typer(
    no_args_is_help=True, help="Let routine read-only commands skip approval review."
)


@approvals_app.command("install")
def install_command(
    extra: list[str] = typer.Option(
        [],
        "--extra",
        help='Additional exact prefix, for example "python3 /abs/tool.py". Repeatable.',
    ),
) -> None:
    """Write the managed allow-rule block into the active Codex home. Safe to repeat."""
    try:
        path, count = install([item.split() for item in extra])
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(f"{count} routine allow rules written to {path}. New sessions pick them up.")


@approvals_app.command("uninstall")
def uninstall_command() -> None:
    """Remove the managed block; rules outside it are untouched."""
    typer.echo("Removed." if uninstall() else "No managed block found.")


@approvals_app.command("status")
def status_command() -> None:
    """Show whether the managed block is installed and which extra prefixes it carries."""
    path = rules_path()
    text = path.read_text() if path.exists() else ""
    installed = BEGIN in text and END in text
    current = (
        installed
        and text.split(BEGIN, 1)[1].split(END, 1)[0].strip()
        == render(existing_extras(text)).split(BEGIN, 1)[1].split(END, 1)[0].strip()
    )
    typer.echo(
        json.dumps(
            {
                "rules": str(path),
                "installed": installed,
                "current": bool(current),
                "extras": existing_extras(text),
            },
            indent=2,
        )
    )


@approvals_app.command("explain")
def explain_command(
    command: str = typer.Argument(..., help="The full command line, quoted."),
) -> None:
    """Explain whether a command line would run without review and how to rephrase it."""
    typer.echo(json.dumps(explain(command), indent=2))


def gh_get(
    path: str = typer.Argument(..., help="REST path such as repos/OWNER/REPO/actions/runs."),
    jq: str = typer.Option("", "--jq", help="jq expression applied by gh."),
    paginate: bool = typer.Option(False, "--paginate", help="Follow pagination."),
) -> None:
    """Run a GitHub REST read. Always GET: it cannot create, change or delete anything."""
    if (
        path.startswith("-")
        or path.strip("/") == "graphql"
        or not re.fullmatch(r"[A-Za-z0-9_./?=&%:+,@-]+", path)
    ):
        raise typer.BadParameter(
            "pass a plain REST path; GraphQL and option-like values are rejected"
        )
    argv = ["gh", "api", "--method", "GET", path]
    if jq:
        argv += ["--jq", jq]
    if paginate:
        argv.append("--paginate")
    raise typer.Exit(subprocess.run(argv, check=False).returncode)


PACKET_READ_COMMANDS = {"startup", "status", "next", "execution", "brief", "audit", "claims"}

def packet_read(
    namespace: str = typer.Option(..., "--namespace"),
    command: str = typer.Argument(...),
    query: str = typer.Option("", "--query"),
    full: bool = typer.Option(False, "--full"),
    packet: str = typer.Option("", "--packet"),
    item: str = typer.Option("", "--item"),
    role: str = typer.Option("worker", "--role"),
) -> None:
    """Read the canonical work tracker; state-changing actions are unavailable."""
    if command not in PACKET_READ_COMMANDS or not re.fullmatch(r"[a-z][a-z0-9-]*", namespace):
        raise typer.BadParameter("Use an explicit namespace and a read-only tracker command")
    if (query or full) and command != "startup":
        raise typer.BadParameter("--query and --full are only valid for startup")
    if packet and command not in {"status", "execution", "brief"}:
        raise typer.BadParameter("--packet is not valid for this read")
    if item and command not in {"execution", "brief"}:
        raise typer.BadParameter("--item is not valid for this read")
    if command in {"execution", "brief"} and (not packet or not item):
        raise typer.BadParameter("execution and brief require --packet and --item")
    if role not in {"worker", "coordinator"} or (role != "worker" and command != "next"):
        raise typer.BadParameter("--role is only valid for next")
    script = Path.home() / "workspace" / "tools" / "scripts" / "work_packets.py"
    argv = [sys.executable, str(script), "--root", str(Path.home() / "workspace"),
            "--namespace", namespace, command]
    if query:
        argv.extend(["--query", query])
    if full:
        argv.append("--full")
    if packet:
        argv.extend(["--packet", packet])
    if item:
        argv.extend(["--item", item])
    if command == "next":
        argv.extend(["--role", role])
    env = {key: value for key, value in os.environ.items() if key != "WORK_PACKETS_ROOT"}
    raise typer.Exit(subprocess.run(argv, env=env, check=False).returncode)
