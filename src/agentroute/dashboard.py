"""Read-only collector behind ``agentroute dashboard``.

Every value comes from state other components already write: Codex's thread, task and goal
databases, each thread's rollout file, AgentRoute's routing audit and waits, Herdr panes and
work packets. Nothing here mutates that state, and a missing source only removes its part of
the view.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import waits
from .config import agentroute_home, codex_home, data_dir

DEFAULT_WINDOW = 24 * 3600
MAX_WINDOW = 14 * 24 * 3600
MAX_AGENTS = 150
# First read of a rollout starts this far from its end; later reads only parse new bytes.
INITIAL_TAIL_BYTES = 1_500_000
RECENT_EVENTS = 80
STALLED_AFTER = 15 * 60
SNAPSHOT_TTL = 1.5
HERDR_TTL = 5.0
OWNER_TTL = 4.0
HERDR_SESSION = "agentroute"  # The session `ar` attaches to; HERDR_SESSION overrides it.
CLAIM_LIMIT = 20  # The tracker refuses a new claim once a namespace holds this many.
STALE_CLAIM = 3 * 24 * 3600
# Who has to act for each waiting step state the work-packet tracker records.
STEP_HOLDS = {"awaiting_user": "you", "waiting_external": "external",
              "waiting_worker": "worker", "coordination_required": "coordination"}
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
STATES = ("needs_input", "working", "waiting", "stalled", "blocked", "idle", "done")

_EVENT_LINE = re.compile(rb'"type":\s*"(event_msg|turn_context)"')
_SECRET_NAME = r"[\w-]*(?:token|secret|passwd|password|api[_-]?key|authorization)[\w-]*"
_SECRET_VALUE = r"(\"[^\"]*\"|'[^']*'|\S+)"
# ``NAME=value`` and ``"name": value`` are masked anywhere. A bare ``name value`` pair is only
# masked for a command-line flag, so prose such as "token usage" stays readable.
_ASSIGNED = re.compile(rf"(?i)\b({_SECRET_NAME}[\"']?\s*[=:]\s*){_SECRET_VALUE}")
_FLAGGED = re.compile(rf"(?i)(--?{_SECRET_NAME}\s+){_SECRET_VALUE}")
_CREDENTIAL = re.compile(r"\b(?:sk-|ghp_|gho_|ghs_|github_pat_|xox[abp]-)[A-Za-z0-9_-]{8,}")
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}")
_REPO_FLAG = re.compile(r"(?:--repo|-R)[ =]([\w.-]+/[\w.-]+)")
_GITHUB_URL = re.compile(r"github\.com[/:]([\w.-]+/[\w.-]+?)(?:\.git|/|\s|$|['\"])")
_PR = re.compile(r"\bgh\s+pr\s+\w+\s+(\d+)\b")
_HOST = re.compile(r"--host[ =]([\w.-]+)")
_SERVICE = re.compile(r"--service[ =]([\w.-]+)")
_URL = re.compile(r"https?://([\w.-]+\.[a-z]{2,})")
_WRAPPERS = {"env", "sudo", "time", "nohup", "command", "exec"}
_SETUP = {"cd", "set", "export", "source", "."}
_KEYWORDS = {"for", "while", "until", "if", "case", "do", "then", "function"}


def redact(text: str, limit: int = 240) -> str:
    """Shorten command text for display and mask values that look like credentials."""
    text = _AUTH_SCHEME.sub(lambda match: f"{match.group(1)} ***", text)
    text = _ASSIGNED.sub(lambda match: f"{match.group(1)}***", text)
    text = _FLAGGED.sub(lambda match: f"{match.group(1)}***", text)
    text = _CREDENTIAL.sub("***", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def short_path(path: str) -> str:
    path = path.removeprefix("file://")
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def _epoch(timestamp: Any) -> float | None:
    if isinstance(timestamp, (int, float)):
        return float(timestamp)
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _rows(path: Path, sql: str, parameters: tuple = ()) -> list[dict[str, Any]]:
    """Query a SQLite file another process owns; an absent or busy file yields no rows."""
    if not path.exists():
        return []
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return []
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, parameters)]
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def program_name(command: str) -> str | None:
    """The first real program in a shell script, past setup lines, wrappers and assignments."""
    for segment in re.split(r"\n|;|&&|\|\|", command):
        words = segment.split()
        wrapped = False
        while words:
            wrapper = words[0].rsplit("/", 1)[-1] in _WRAPPERS
            if not (wrapper or re.match(r"[A-Za-z_]\w*=", words[0])
                    or (wrapped and words[0].startswith("-"))):
                break
            wrapped = wrapped or wrapper
            words.pop(0)
        if not words or words[0] in _SETUP:
            continue
        name = words[0].strip("'\"()").rsplit("/", 1)[-1]
        if name in _KEYWORDS or not re.fullmatch(r"[\w.+-]{1,40}", name):
            return None
        return name
    return None


def command_targets(script: str) -> list[tuple[str, str]]:
    """What a shell command reaches outside the local checkout."""
    targets: list[tuple[str, str]] = []
    repositories = _REPO_FLAG.findall(script) + _GITHUB_URL.findall(script)
    pulls = _PR.findall(script)
    for repository in dict.fromkeys(repositories):
        targets.append(("github", repository))
    for number in dict.fromkeys(pulls):
        label = f"{repositories[0]}#{number}" if repositories else f"PR #{number}"
        targets.append(("pr", label))
    targets.extend(("host", host) for host in dict.fromkeys(_HOST.findall(script)))
    targets.extend(("service", name) for name in dict.fromkeys(_SERVICE.findall(script)))
    for host in dict.fromkeys(_URL.findall(script)):
        if host not in {"github.com", "api.github.com"}:
            targets.append(("web", host))
    return targets


class RolloutTail:
    """Incrementally reads one thread's rollout and keeps what the dashboard shows."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.identity: tuple[int, int] | None = None
        self.partial = False
        self._skip_line = False
        self.turn: dict[str, Any] | None = None
        self.last_turn: dict[str, Any] | None = None
        self.tools: Counter[str] = Counter()
        self.programs: Counter[str] = Counter()
        self.targets: Counter[tuple[str, str]] = Counter()
        self.recent: deque[dict[str, Any]] = deque(maxlen=RECENT_EVENTS)
        self.tokens: dict[str, Any] = {}
        self.model: str | None = None
        self.effort: str | None = None
        self.message: dict[str, Any] | None = None
        self.failures = 0
        self.compactions = 0
        self.turns_completed = 0
        self.updated_at: float | None = None

    def refresh(self) -> None:
        try:
            status = self.path.stat()
        except OSError:
            return
        identity = (status.st_ino, status.st_dev)
        if identity != self.identity or status.st_size < self.offset:
            self.__init__(self.path)
            self.identity = identity
            if status.st_size > INITIAL_TAIL_BYTES:
                self.offset = status.st_size - INITIAL_TAIL_BYTES
                self.partial = True
                self._skip_line = True
        if status.st_size == self.offset:
            return
        self.updated_at = status.st_mtime
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            if self._skip_line:
                handle.readline()  # The first line after a mid-file seek is incomplete.
                self.offset = handle.tell()
                self._skip_line = False
            for line in handle:
                if not line.endswith(b"\n"):
                    break  # A writer is mid-line; read it on the next refresh.
                self.offset = handle.tell()
                if not _EVENT_LINE.search(line[:200]):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                payload = record.get("payload")
                if isinstance(payload, dict):
                    self._apply(record.get("type"), payload, _epoch(record.get("timestamp")))

    def _apply(self, record_type: str | None, payload: dict[str, Any], at: float | None) -> None:
        if record_type == "turn_context":
            self.model = payload.get("model") or self.model
            self.effort = payload.get("effort") or self.effort
            return
        kind = payload.get("type")
        if kind == "task_started":
            attribution = payload.get("turn_attribution") or {}
            self.turn = {
                "id": payload.get("turn_id"),
                "started_at": payload.get("started_at") or at,
                "trigger": attribution.get("turn_trigger"),
                "tools": 0,
            }
        elif kind in {"task_complete", "turn_aborted"}:
            self.last_turn = {
                "id": payload.get("turn_id"),
                "outcome": "completed" if kind == "task_complete" else
                payload.get("reason") or "aborted",
                "completed_at": payload.get("completed_at") or at,
                "duration_ms": payload.get("duration_ms"),
                "tools": (self.turn or {}).get("tools", 0),
            }
            self.turns_completed += kind == "task_complete"
            self.turn = None
        elif kind == "token_count":
            info = payload.get("info") or {}
            limits = (payload.get("rate_limits") or {}).get("primary") or {}
            self.tokens = {
                "total": (info.get("total_token_usage") or {}).get("total_tokens"),
                "context_used": (info.get("last_token_usage") or {}).get("input_tokens"),
                "context_window": info.get("model_context_window"),
                "rate_limit_used_percent": limits.get("used_percent"),
            }
        elif kind == "item_completed":
            self._open_turn_before_tail()
            self._item(payload.get("item") or {}, at)

    def _open_turn_before_tail(self) -> None:
        """Items only happen inside a turn, so one seen first means the turn began earlier."""
        if self.partial and self.turn is None and self.last_turn is None:
            self.turn = {"id": None, "started_at": None, "trigger": None, "tools": 0}

    def _item(self, item: dict[str, Any], at: float | None) -> None:
        kind = item.get("type")
        event: dict[str, Any] | None = None
        if kind == "CommandExecution":
            argv = item.get("command") or []
            script = argv[-1] if isinstance(argv, list) and argv else str(argv)
            directory = short_path(str(item.get("cwd") or ""))
            parsed = item.get("parsed_cmd") or [{"cmd": script}]
            programs = [program_name(str(entry.get("cmd") or "")) for entry in parsed]
            programs = list(dict.fromkeys(name for name in programs if name)) or ["shell"]
            self.programs.update(programs)
            self.tools["shell"] += 1
            if directory:
                self.targets[("dir", directory)] += 1
            self.targets.update(command_targets(script))
            ok = item.get("exit_code") in (0, None)
            self.failures += not ok
            duration = item.get("duration") or {}
            event = {"tool": "shell", "label": " ".join(programs),
                     "detail": redact(script), "ok": ok, "where": directory,
                     "seconds": round(duration.get("secs", 0) + duration.get("nanos", 0) / 1e9, 1)}
        elif kind == "FileChange":
            paths = [short_path(path) for path in (item.get("changes") or {})]
            self.tools["apply_patch"] += 1
            self.targets.update(("file", path) for path in paths)
            event = {"tool": "apply_patch", "label": f"{len(paths)} file(s)",
                     "detail": ", ".join(paths)[:240], "ok": item.get("status") == "completed"}
        elif kind == "McpToolCall":
            server, tool = str(item.get("server") or "mcp"), str(item.get("tool") or "")
            self.tools[f"mcp:{server}"] += 1
            self.targets[("mcp", server)] += 1
            event = {"tool": f"mcp:{server}", "label": tool, "detail": "",
                     "ok": item.get("status") == "completed"}
        elif kind == "Extension":
            name = str(item.get("kind") or "extension")
            self.tools[name] += 1
            detail = str(item.get("query") or "")
            self.targets.update(("web", host) for host in _URL.findall(detail)[:1])
            event = {"tool": name, "label": name, "detail": redact(detail), "ok": True}
        elif kind == "SubAgentActivity":
            child = str(item.get("agent_thread_id") or "")
            self.tools["subagent"] += 1
            if child:
                self.targets[("agent", child)] += 1
            event = {"tool": "subagent", "label": str(item.get("kind") or ""),
                     "detail": str(item.get("agent_path") or ""), "ok": True}
        elif kind == "ContextCompaction":
            self.compactions += 1
            event = {"tool": "compaction", "label": "context compacted", "detail": "", "ok": True}
        elif kind == "AgentMessage":
            text = " ".join(
                str(part.get("text") or "") for part in item.get("content") or []
                if isinstance(part, dict)
            ).strip()
            # AgentRoute's own route banner is not something the agent said.
            if text and not str(item.get("id") or "").startswith("agentroute-"):
                self.message = {"at": at, "phase": item.get("phase"), "text": redact(text, 400)}
        if event:
            event["at"] = at
            self.recent.append(event)
            if self.turn is not None:
                self.turn["tools"] += 1

    def view(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "last_turn": self.last_turn,
            "tools": dict(self.tools.most_common()),
            "programs": dict(self.programs.most_common(15)),
            "targets": [
                {"kind": kind, "label": label, "count": count}
                for (kind, label), count in self.targets.most_common(25)
            ],
            "tokens": self.tokens,
            "message": self.message,
            "failures": self.failures,
            "compactions": self.compactions,
            "turns_completed": self.turns_completed,
            "partial": self.partial,
            "last_event": self.recent[-1] if self.recent else None,
        }


def thread_kind(source: str) -> tuple[str, dict[str, Any]]:
    """Classify a thread from Codex's ``source`` column: user root, worker or reviewer."""
    if not source.startswith("{"):
        return ("exec" if source == "exec" else "root"), {}
    try:
        subagent = json.loads(source).get("subagent")
    except ValueError:
        return "root", {}
    if subagent == "review":
        return "review", {}
    if isinstance(subagent, dict) and "thread_spawn" in subagent:
        return "worker", subagent["thread_spawn"] or {}
    if isinstance(subagent, dict) and subagent.get("other") == "guardian":
        return "guardian", {}
    return "review", {}


def packets_root() -> Path:
    return Path(os.environ.get("WORK_PACKETS_ROOT") or Path.home() / "workspace")


def default_names() -> list[str]:
    """The reader's first name, from git, to recognise holds recorded as theirs."""
    try:
        name = subprocess.run(["git", "config", "--get", "user.name"], capture_output=True,
                              text=True, timeout=3).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []
    return name[:1]


def names_person(owner: str, names: list[str]) -> bool:
    words = {word.casefold() for word in re.findall(r"[^\W\d_]+", owner)}
    return any(name.casefold() in words for name in names)


def _hold(kind: str, wait: dict[str, Any], fallback: str, names: list[str],
          blocking: bool) -> dict[str, Any]:
    owner = str(wait.get("owner") or "")
    return {
        "kind": "you" if names_person(owner, names) else kind,
        "blocking": blocking,
        "owner": redact(owner, 120),
        "reason": redact(str(wait.get("reason") or fallback or ""), 420),
        "unblock": redact(str(wait.get("resume_when") or ""), 420),
        "question": redact(str(wait.get("question_ref") or ""), 160),
    }


def item_hold(item: dict[str, Any], names: list[str]) -> dict[str, Any] | None:
    """Why an unfinished outcome cannot move, who has to act, and what clears it.

    The answer comes only from fields the tracker validates: the current step's waiting
    state, the hold on the outcome, and each one's named owner. An outcome that is blocked
    with nothing but free text has no recorded owner, and is reported as exactly that.
    """
    status = item.get("status")
    if status in {"done", "cancelled"}:
        return None
    step, later = item.get("next_step") or {}, item.get("outcome_wait") or {}
    blocker = str(item.get("blocker") or "")
    if step.get("state") in STEP_HOLDS:
        return _hold(STEP_HOLDS[step["state"]], step.get("wait") or {}, blocker, names, True)
    executable = step.get("state") in {"ready", "active"}
    if later.get("state") in STEP_HOLDS:
        # Work can continue on the current step; this hold applies before the outcome closes.
        return _hold(STEP_HOLDS[later["state"]], later, blocker, names,
                     blocking=status == "blocked" and not executable)
    if status == "blocked" and not executable:
        return _hold("unowned", {}, blocker, names, True)
    return None


def load_packets(cutoff: float, names: list[str] | None = None) -> list[dict[str, Any]]:
    """Packets with unfinished outcomes, or any change inside the window."""
    names = names or []
    packets = []
    for path in sorted((packets_root() / "tasks" / "packets").glob("*/*.json")):
        try:
            packet = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        items = []
        for item in packet.get("items") or []:
            criteria = item.get("criteria") or []
            checkpoint = item.get("checkpoint") or {}
            step = item.get("next_step") or {}
            gates = item.get("gates") or {}
            items.append({
                "id": item.get("id"),
                "revision": item.get("revision"),
                "objective": redact(str(item.get("objective") or ""), 220),
                "status": item.get("status"),
                "priority": item.get("priority"),
                "step_state": step.get("state"),
                "next_action": redact(
                    str(item.get("next_action") or step.get("action") or ""), 220),
                "session": (item.get("assignment") or {}).get("session"),
                "assigned_at": _epoch((item.get("assignment") or {}).get("at")),
                "hold": item_hold(item, names),
                "review_at": _epoch(item.get("next_review")),
                "checkpoint": redact(str(checkpoint.get("summary") or ""), 220),
                "checkpoint_at": _epoch(checkpoint.get("at")),
                "criteria_met": sum(bool(entry.get("evidence")) for entry in criteria),
                "criteria_total": len(criteria),
                "gates": {name: (gate or {}).get("status") for name, gate in gates.items()
                          if isinstance(gate, dict) or gate is None},
            })
        updated_at = _epoch(packet.get("updated_at")) or path.stat().st_mtime
        unfinished = any(item["status"] not in {"done", "cancelled"} for item in items)
        if unfinished or updated_at >= cutoff:
            packets.append({
                "namespace": packet.get("namespace") or path.parent.name,
                "id": packet.get("id") or path.stem,
                "objective": redact(str(packet.get("objective") or ""), 260),
                "updated_at": updated_at,
                "items": items,
            })
    return sorted(packets, key=lambda packet: packet["updated_at"], reverse=True)


def herdr_panes() -> dict[str, dict[str, Any]]:
    """Herdr panes keyed by the thread each one shows."""
    session = os.environ.get("HERDR_SESSION") or HERDR_SESSION

    def call(*arguments: str) -> dict[str, Any]:
        result = subprocess.run(["herdr", "--session", session, *arguments],
                                capture_output=True, text=True, timeout=5)
        return json.loads(result.stdout).get("result") or {}

    try:
        agents = call("agent", "list").get("agents") or []
        workspaces = call("workspace", "list").get("workspaces") or []
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    labels = {workspace["workspace_id"]: workspace.get("label") for workspace in workspaces}
    # Pane IDs repeat across Herdr sessions, so only this session's records apply.
    key = hashlib.sha256(session.encode()).hexdigest()[:16]
    saved: dict[tuple[str, str], tuple[float, str]] = {}
    for path in agentroute_home().glob(f"shared-server/*/herdr-layout/{key}/*.json"):
        try:
            record = json.loads(path.read_text())
            modified = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        pane, terminal = record.get("pane_id"), record.get("terminal_id")
        if pane and terminal and record.get("thread"):
            identity = (pane, terminal)
            if modified >= saved.get(identity, (0, ""))[0]:
                saved[identity] = (modified, record["thread"])
    panes = {}
    for agent in agents:
        reported = agent.get("agent_session") or {}
        identity = (agent.get("pane_id"), agent.get("terminal_id"))
        thread = (reported.get("value") if reported.get("kind") == "id" else None) \
            or saved.get(identity, (0, ""))[1]
        if thread:
            panes[thread] = {
                "pane_id": agent.get("pane_id"),
                "workspace": labels.get(agent.get("workspace_id")) or agent.get("workspace_id"),
                "status": agent.get("agent_status"),
                "title": agent.get("terminal_title_stripped"),
                "focused": bool(agent.get("focused")),
            }
    return panes


def owner_states() -> dict[str, str] | None:
    """Live state of every thread the shared Codex owner has loaded; None when unreachable.

    This is the authority on whether a turn is running or waiting on a person. Rollout files
    lag it, and they never record a turn that died with its process.
    """
    try:
        from .shared_server import RpcClient, socket_path

        states = {}
        with RpcClient(socket_path(), name="agentroute_dashboard", timeout=3) as client:
            for thread_id in client.request("thread/loaded/list", {}).get("data") or []:
                status = client.request("thread/read", {"threadId": thread_id})["thread"].get(
                    "status") or {}
                flags = status.get("activeFlags") or []
                states[thread_id] = (
                    "approval" if "waitingOnApproval" in flags
                    else "input" if "waitingOnUserInput" in flags
                    else {"active": "running", "idle": "idle", "systemError": "error"}.get(
                        status.get("type"), "unknown"))
        return states
    except Exception:  # noqa: BLE001
        return None  # Not running, another runtime version, or a direct (unshared) setup.


def agent_state(agent: dict[str, Any], now: float) -> tuple[str, str]:
    """One state per agent, ordered by how soon a person needs to look at it."""
    pane, goal, task = agent.get("pane") or {}, agent.get("goal") or {}, agent.get("task") or {}
    turn, live = agent["activity"].get("turn"), agent.get("live")
    if live == "approval":
        return "needs_input", "waiting for you to approve an action"
    if live == "input":
        return "needs_input", "asked you a question"
    if pane.get("status") == "blocked":
        return "needs_input", "Herdr reports the pane is waiting for you"
    if live == "error":
        return "blocked", "the thread stopped on a system error"
    if live in {"idle", "unknown"}:
        turn = None  # The owner says no turn is running, whatever the rollout last recorded.
    elif live == "running" and not turn:
        turn = {"started_at": None, "tools": 0}
    if turn:
        quiet = now - (agent.get("active_at") or now)
        if quiet > STALLED_AFTER:
            return "stalled", f"turn open with no activity for {int(quiet // 60)} min"
        return "working", "turn in progress"
    if agent["waits"]:
        return "waiting", agent["waits"][0]["description"]
    if goal.get("status") in {"blocked", "usage_limited", "budget_limited"}:
        return "blocked", f"goal {goal['status'].replace('_', ' ')}"
    if task.get("status") == "waiting":
        return "waiting", task.get("summary") or "task waiting"
    if goal.get("status") == "complete" or task.get("status") == "completed":
        return "done", task.get("summary") or "goal complete"
    last_turn = agent["activity"].get("last_turn") or {}
    if agent["kind"] == "exec" and last_turn.get("outcome") == "completed":
        return "done", "one-shot run finished"
    if goal.get("status") == "paused":
        return "idle", "goal paused"
    return "idle", "no turn running"


def _focus_order(entry: dict[str, Any]) -> tuple:
    return (not entry["blocking"], not entry.get("asking"),
            PRIORITY_ORDER.get(entry.get("priority") or "", 9),
            entry.get("since") or 0)


def build_focus(packets: list[dict[str, Any]], agents: list[dict[str, Any]],
                failed_waits: list[dict[str, Any]], names: list[str],
                now: float) -> dict[str, Any]:
    """The short list a person needs: what waits on them, and why everything else is stuck."""
    by_id = {agent["id"]: agent for agent in agents}
    needs_you, others, unowned = [], {}, []
    claims: dict[str, dict[str, Any]] = {}
    for packet in packets:
        namespace = claims.setdefault(packet["namespace"], {
            "namespace": packet["namespace"], "claimed": 0, "limit": CLAIM_LIMIT, "held": 0,
            "stale": 0, "ready": 0})
        for item in packet["items"]:
            if item["status"] in {"done", "cancelled"}:
                continue
            hold, since = item["hold"], item["checkpoint_at"] or packet["updated_at"]
            if item["session"]:
                namespace["claimed"] += 1
                namespace["held"] += bool(hold and hold["blocking"])
                namespace["stale"] += now - since > STALE_CLAIM
            elif item["status"] == "ready" and not hold:
                namespace["ready"] += 1
            if not hold:
                continue
            entry = {
                **hold, "type": "outcome", "title": item["objective"] or item["id"],
                "namespace": packet["namespace"], "packet": packet["id"], "item": item["id"],
                "revision": item.get("revision"), "priority": item["priority"],
                "next_action": item["next_action"],
                "since": since, "review_at": item["review_at"],
                "review_overdue": bool(item["review_at"] and item["review_at"] < now),
                "session": item["session"],
                "agent": item["session"] if item["session"] in by_id else None,
            }
            if hold["kind"] == "you":
                needs_you.append(entry)
            elif hold["kind"] == "unowned":
                unowned.append(entry)
            elif hold["blocking"]:
                others.setdefault(hold["kind"], []).append(entry)
    for agent in agents:
        goal = agent.get("goal") or {}
        limited = goal.get("status") in {"usage_limited", "budget_limited"}
        if agent["state"] not in {"needs_input", "stalled", "blocked"}:
            continue
        asking = agent["state"] == "needs_input"
        entries = needs_you if asking else others.setdefault("coordination", [])
        entries.append({
            "kind": "you" if asking else "coordination", "asking": asking,
            "blocking": True, "type": "agent", "agent": agent["id"],
            "title": agent["title"] or "(untitled)", "priority": None, "owner": "",
            "reason": agent["state_reason"],
            "unblock": "Raise the goal's budget or wait for the usage limit to reset."
            if limited else "Open the session and answer it."
            if agent["state"] == "needs_input" else "Open the session and check on it.",
            "since": agent.get("active_at"), "session": agent["id"],
            "pane": (agent.get("pane") or {}).get("pane_id"),
            "workspace": (agent.get("pane") or {}).get("workspace"),
            "detail": redact(str((agent.get("task") or {}).get("summary")
                                 or ((agent["activity"].get("message") or {}).get("text"))
                                 or ""), 300),
        })
    for wait in failed_waits:
        others.setdefault("coordination", []).append({
            "kind": "coordination", "blocking": True, "type": "wait", "title": wait["description"],
            "priority": None, "owner": "", "agent": wait["thread_id"]
            if wait["thread_id"] in by_id else None, "session": wait["thread_id"],
            "reason": "This wait is past its deadline and has not woken its session."
            if wait["state"] == "pending" else "The wake could not be delivered to its session.",
            "unblock": "Open the session; `agentroute wait show " + str(wait["id"])
                       + "` has the details.",
            "since": wait["deadline_at"] or wait["created_at"],
        })
    groups = [{"kind": kind, "items": sorted(others[kind], key=_focus_order)}
              for kind in ("coordination", "worker", "external") if kind in others]
    return {
        "names": names,
        "needs_you": sorted(needs_you, key=_focus_order),
        "others": groups,
        "unowned": sorted(unowned, key=_focus_order),
        "claims": sorted((entry for entry in claims.values() if entry["claimed"]),
                         key=lambda entry: -entry["claimed"]),
        "counts": {"needs_you": len(needs_you), "unowned": len(unowned),
                   "others": sum(len(group["items"]) for group in groups),
                   "blocking_you": sum(entry["blocking"] for entry in needs_you)},
    }


class Collector:
    """Builds snapshots and caches the parts that are expensive to recompute."""

    def __init__(self, names: list[str] | None = None) -> None:
        self.names = default_names() if names is None else names
        self._lock = threading.Lock()
        self._tails: dict[str, RolloutTail] = {}
        self._snapshots: dict[int, tuple[float, dict[str, Any]]] = {}
        self._herdr: tuple[float, dict[str, dict[str, Any]]] = (0.0, {})
        self._owner: tuple[float, dict[str, str] | None] = (0.0, None)

    def snapshot(self, window: int = DEFAULT_WINDOW) -> dict[str, Any]:
        window = max(300, min(int(window), MAX_WINDOW))
        with self._lock:
            cached = self._snapshots.get(window)
            if cached and time.time() - cached[0] < SNAPSHOT_TTL:
                return cached[1]
            snapshot = self._build(window, time.time())
            self._snapshots = {window: (time.time(), snapshot)}
            return snapshot

    def _panes(self, now: float) -> dict[str, dict[str, Any]]:
        if now - self._herdr[0] >= HERDR_TTL:
            self._herdr = (now, herdr_panes())
        return self._herdr[1]

    def _live(self, now: float) -> dict[str, str] | None:
        if now - self._owner[0] >= OWNER_TTL:
            self._owner = (now, owner_states())
        return self._owner[1]

    def _tail(self, rollout: str) -> RolloutTail:
        tail = self._tails.get(rollout)
        if tail is None:
            tail = self._tails[rollout] = RolloutTail(Path(rollout))
        tail.refresh()
        return tail

    def _build(self, window: int, now: float) -> dict[str, Any]:
        cutoff = now - window
        state = codex_home() / "state_5.sqlite"
        all_waits = _load_waits()
        pending = [wait for wait in all_waits if wait.get("state") == "pending"]
        waiting_threads = sorted({wait["thread_id"] for wait in pending if wait.get("thread_id")})
        threads = _rows(state, "SELECT * FROM threads WHERE updated_at >= ? AND archived = 0 "
                               "ORDER BY updated_at DESC LIMIT 2000", (int(cutoff),))
        known = {thread["id"] for thread in threads}
        for thread_id in waiting_threads:
            if thread_id not in known:
                threads += _rows(state, "SELECT * FROM threads WHERE id = ?", (thread_id,))
        tasks = {row["thread_id"]: row for row in _rows(state, "SELECT * FROM task_work")}
        goals = {row["thread_id"]: row for row in _rows(
            codex_home() / "goals_1.sqlite",
            "SELECT g.*, r.decision AS review_decision, r.reason AS review_reason, "
            "r.attempts AS review_attempts FROM thread_goals g "
            "LEFT JOIN thread_goal_completion_reviews r "
            "ON r.thread_id = g.thread_id AND r.goal_id = g.goal_id")}
        routes = _rows(data_dir() / "audit.db",
                       "SELECT * FROM routing_decisions WHERE created_at >= ? ORDER BY id",
                       (datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),))
        usage, per_thread = summarize_routes(routes, cutoff, window)
        panes = self._panes(now)
        live = self._live(now)
        packets = load_packets(cutoff, self.names)
        assigned: dict[str, list[dict[str, Any]]] = {}
        for packet in packets:
            for item in packet["items"]:
                if item["session"] and item["status"] not in {"done", "cancelled"}:
                    assigned.setdefault(item["session"], []).append({
                        "namespace": packet["namespace"], "packet": packet["id"],
                        "item": item["id"], "status": item["status"],
                        "next_action": item["next_action"]})

        agents, reviewers, empty = [], {"count": 0, "tokens": 0, "by_kind": Counter()}, 0
        for thread in threads:
            kind, spawn = thread_kind(str(thread.get("source") or ""))
            if kind in {"review", "guardian"}:
                reviewers["count"] += 1
                reviewers["tokens"] += thread.get("tokens_used") or 0
                reviewers["by_kind"][kind] += 1
                continue
            if len(agents) >= MAX_AGENTS:
                continue
            thread_id = thread["id"]
            tail = self._tail(thread["rollout_path"]) if thread.get("rollout_path") else None
            activity = tail.view() if tail else RolloutTail(Path(os.devnull)).view()
            route = per_thread.get(thread_id, {})
            task, goal = tasks.get(thread_id), goals.get(thread_id)
            title = (task or {}).get("title") or thread.get("name") or thread.get("title") or ""
            path = spawn.get("agent_path") or thread.get("agent_path") or ""
            nickname = spawn.get("agent_nickname") or thread.get("agent_nickname") or ""
            if not title and kind == "worker":
                # Workers have no title of their own; the supervisor names them by path.
                title = " · ".join(part for part in (nickname, path.rsplit("/", 1)[-1]) if part)
            agent = {
                "id": thread_id,
                "kind": kind,
                "parent": spawn.get("parent_thread_id"),
                "path": path or None,
                "nickname": nickname or None,
                "role": spawn.get("agent_role") or thread.get("agent_role"),
                "title": redact(str(title), 140),
                "cwd": short_path(str(thread.get("cwd") or "")),
                "branch": thread.get("git_branch"),
                "model": route.get("model") or (tail.model if tail else None)
                or thread.get("model"),
                "effort": route.get("effort") or (tail.effort if tail else None)
                or thread.get("reasoning_effort"),
                "backend": route.get("backend"),
                "tier": route.get("tier"),
                "task_summary": route.get("task_summary"),
                "tokens_total": thread.get("tokens_used") or 0,
                "tokens_window": route.get("tokens", 0),
                "turns_window": route.get("turns", 0),
                "created_at": thread.get("created_at"),
                "updated_at": thread.get("updated_at"),
                "active_at": max(thread.get("updated_at") or 0,
                                 (tail.updated_at if tail else None) or 0),
                "task": task and {"title": task.get("title"), "summary": task.get("summary"),
                                  "status": task.get("status"),
                                  "updated_at": task.get("updated_at")},
                "goal": goal and {
                    "objective": redact(str(goal.get("objective") or ""), 600),
                    "status": goal.get("status"),
                    "tokens_used": goal.get("tokens_used"),
                    "token_budget": goal.get("token_budget"),
                    "seconds_used": goal.get("time_used_seconds"),
                    "updated_at": (goal.get("updated_at_ms") or 0) / 1000,
                    "review_decision": goal.get("review_decision"),
                    "review_reason": goal.get("review_reason"),
                },
                "waits": [wait for wait in map(_wait_view, pending)
                          if wait["thread_id"] == thread_id],
                "pane": panes.get(thread_id),
                # Direct sessions and threads missing from the owner list use rollout evidence.
                "live": (live or {}).get(thread_id),
                "packets": assigned.get(thread_id, []),
                "activity": activity,
                "children": [],
            }
            agent["state"], agent["state_reason"] = agent_state(agent, now)
            if agent["state"] == "idle" and not (
                agent["tokens_total"] or agent["waits"] or agent["pane"] or agent["goal"]
            ):
                empty += 1  # A thread that was opened and never used tells the reader nothing.
                continue
            agents.append(agent)

        by_id = {agent["id"]: agent for agent in agents}
        for agent in agents:
            if agent["parent"] in by_id:
                by_id[agent["parent"]]["children"].append(agent["id"])
        counts = Counter(agent["state"] for agent in agents)
        attention = [
            {"agent": agent["id"], "title": agent["title"], "state": agent["state"],
             "reason": agent["state_reason"]}
            for agent in agents if agent["state"] in {"needs_input", "stalled", "blocked"}
        ]
        overdue = [wait for wait in map(_wait_view, all_waits)
                   if wait["state"] == "undeliverable"
                   or (wait["state"] == "pending" and wait["deadline_at"] < now)]
        attention += [{"agent": wait["thread_id"], "title": wait["description"],
                       "state": "wait", "reason": f"wait {wait['state']} past its deadline"
                       if wait["state"] == "pending" else "wake could not be delivered"}
                      for wait in overdue]
        return {
            "generated_at": now,
            "window": window,
            "summary": {
                **{state: counts.get(state, 0) for state in STATES},
                "agents": len(agents),
                "tokens": usage["tokens"],
                "turns": usage["turns"],
                "pending_waits": len(pending),
                "open_packet_items": sum(
                    item["status"] not in {"done", "cancelled"}
                    for packet in packets for item in packet["items"]),
                "reviewers": {"count": reviewers["count"], "tokens": reviewers["tokens"],
                              "by_kind": dict(reviewers["by_kind"])},
                "unused_threads": empty,
            },
            "agents": agents,
            "attention": attention,
            "waits": sorted(
                (wait for wait in map(_wait_view, all_waits)
                 if wait["state"] == "pending" or wait["created_at"] >= cutoff),
                key=lambda wait: (wait["state"] != "pending", -wait["created_at"])),
            "packets": packets,
            "focus": build_focus(packets, agents, overdue, self.names, now),
            "usage": usage,
            "sources": {
                "codex_threads": state.exists(),
                "codex_goals": (codex_home() / "goals_1.sqlite").exists(),
                "routing_audit": (data_dir() / "audit.db").exists(),
                "herdr": bool(panes),
                "shared_owner": live is not None,
                "work_packets": (packets_root() / "tasks" / "packets").is_dir(),
            },
        }

    def agent(self, thread_id: str, window: int = DEFAULT_WINDOW) -> dict[str, Any] | None:
        """Everything known about one agent: its turns, recent tool calls and waits."""
        snapshot = self.snapshot(window)
        agent = next((agent for agent in snapshot["agents"] if agent["id"] == thread_id), None)
        if agent is None:
            return None
        rollout = _rows(codex_home() / "state_5.sqlite",
                        "SELECT rollout_path FROM threads WHERE id = ?", (thread_id,))
        with self._lock:
            tail = self._tails.get(rollout[0]["rollout_path"]) if rollout else None
            events = list(tail.recent) if tail else []
        routes = _rows(data_dir() / "audit.db",
                       "SELECT * FROM routing_decisions WHERE session_id = ? OR agent_id = ? "
                       "ORDER BY id DESC LIMIT 200", (thread_id, thread_id))
        turns = [view for owner, view in collapse_turns(routes[::-1]) if owner == thread_id]
        return {
            **agent,
            "events": events[::-1],
            "turns": turns[::-1][:40],
            "wait_history": [wait for wait in map(_wait_view, _load_waits())
                             if wait["thread_id"] == thread_id][-25:][::-1],
        }


def _load_waits() -> list[dict[str, Any]]:
    try:
        return waits.load()
    except (OSError, ValueError):
        return []


def _wait_view(wait: dict[str, Any]) -> dict[str, Any]:
    try:
        description = waits.describe(wait)
    except (KeyError, TypeError):
        description = f"{wait.get('id')} {wait.get('kind')}"
    return {
        "id": wait.get("id"),
        "thread_id": wait.get("thread_id"),
        "kind": wait.get("kind"),
        "state": wait.get("state"),
        "description": description,
        "note": redact(str(wait.get("note") or ""), 200),
        "created_at": wait.get("created_at") or 0,
        "deadline_at": wait.get("deadline_at") or 0,
        "last_check": redact(str((wait.get("last_check") or {}).get("summary") or ""), 160),
        "last_check_at": (wait.get("last_check") or {}).get("at"),
        "outcome": (wait.get("outcome") or {}).get("reason"),
        "disposition": (wait.get("disposition") or {}).get("action"),
    }


def _route_thread(route: dict[str, Any]) -> str:
    """Subagent routes are recorded under the root session with the worker in ``agent_id``."""
    return route.get("agent_id") or route.get("session_id") or ""


def _turn_view(route: dict[str, Any]) -> dict[str, Any]:
    try:
        reasons = json.loads(route.get("reason_codes") or "[]")
    except ValueError:
        reasons = []
    return {
        "at": _epoch(route.get("created_at")),
        "tier": route.get("selected_tier"),
        "model": route.get("actual_model") or route.get("model"),
        "effort": route.get("actual_reasoning_effort") or route.get("reasoning_effort"),
        "backend": route.get("actual_backend") or route.get("backend"),
        "outcome": route.get("turn_outcome"),
        "tokens": route.get("answer_total_tokens"),
        "input_tokens": route.get("answer_input_tokens"),
        "cached_tokens": route.get("answer_cached_input_tokens"),
        "output_tokens": route.get("answer_output_tokens"),
        "duration_ms": route.get("turn_duration_ms"),
        "task_type": route.get("classifier_task_type"),
        "summary": route.get("task_summary"),
        "reasons": reasons,
    }


_TOKEN_FIELDS = ("tokens", "input_tokens", "cached_tokens", "output_tokens")


def collapse_turns(routes: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """One ``(thread, turn)`` pair per turn, in the order the turns were first routed.

    The audit holds a row for every routing decision, and a turn can have several: steering
    messages, wakes and continuations each add one. Token counts on those rows are running
    totals for the turn, so the largest is the turn's use and adding them would count twice.
    """
    turns: dict[tuple[str, Any], dict[str, Any]] = {}
    for position, route in enumerate(routes):
        thread, view = _route_thread(route), _turn_view(route)
        key = (thread, route.get("turn_id") or f"row-{position}")
        merged = turns.get(key)
        if merged is None:
            turns[key] = view
            continue
        for name, value in view.items():
            if name in _TOKEN_FIELDS:
                merged[name] = max(merged[name] or 0, value or 0) or None
            elif value not in (None, "", []):
                merged[name] = value
    return [(thread, view) for (thread, _), view in turns.items()]


def summarize_routes(
    routes: list[dict[str, Any]], cutoff: float, window: int
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Token use over time by model, and the latest route and totals for each thread."""
    bucket_seconds = max(60, window // 48)
    buckets: dict[int, Counter[str]] = {}
    models: dict[str, dict[str, Any]] = {}
    threads: dict[str, dict[str, Any]] = {}
    turns = collapse_turns(routes)
    for thread_id, view in turns:
        tokens = view["tokens"] or 0
        model = view["model"] or "unknown"
        at = view["at"] or cutoff
        index = max(0, int((at - cutoff) // bucket_seconds))
        buckets.setdefault(index, Counter())[model] += tokens
        entry = models.setdefault(model, {"model": model, "backend": view["backend"],
                                          "tokens": 0, "turns": 0, "cached": 0})
        entry["tokens"] += tokens
        entry["turns"] += 1
        entry["cached"] += view["cached_tokens"] or 0
        thread = threads.setdefault(thread_id, {"tokens": 0, "turns": 0})
        thread["tokens"] += tokens
        thread["turns"] += 1
        thread.update(model=view["model"], effort=view["effort"], backend=view["backend"],
                      tier=view["tier"], task_summary=view["summary"])
    usage = {
        "bucket_seconds": bucket_seconds,
        "start": cutoff,
        "buckets": [{"at": cutoff + index * bucket_seconds, "models": dict(counter)}
                    for index, counter in sorted(buckets.items())],
        "models": sorted(models.values(), key=lambda entry: entry["tokens"], reverse=True),
        "tokens": sum(entry["tokens"] for entry in models.values()),
        "turns": len(turns),
    }
    return usage, threads
