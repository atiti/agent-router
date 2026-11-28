"""Setup-provenance diagnostics for `agentroute doctor`.

Two installations with the same configuration can still behave differently when their
environment drifted: a package installed from a local checkout, a hand-edited module, a
private runtime started by a terminal multiplexer, processes still running a replaced
binary, a shared server started by an older build, or computer-use tooling that points at
a re-signed copy of an app. These checks make that drift visible.

Every check is local, bounded, and read-only. Details never contain credentials, account
identifiers, emails, prompts, hostnames, or absolute home paths.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import Distribution, PackageNotFoundError, distribution
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

from .config import AppConfig, agentroute_home, codex_home
from .install_manifest import RUNTIME_FILES, read_manifest, sha256_file

# Runtime revision that first shipped portable cross-provider compaction for routed
# histories that need provider-state isolation (Codex 0.172 port, AgentRoute 0.5.87;
# see docs/codex-fork.md "Stable 0.172.0 port"). Older runtimes can send a foreign
# opaque compaction checkpoint to the wrong provider in long multi-provider threads.
MIN_PORTABLE_COMPACTION_REVISION = 72
FINGERPRINT_SCHEMA = 1
SUBPROCESS_TIMEOUT = 3.0
STALE_LOGIN_DAYS = 30
MAX_COMPUTER_USE_PATHS = 12

_REVISION = re.compile(r"provider-routing-v(\d+)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_USER_DIR = re.compile(r"/(?:Users|home)/[^/\s\"':]+")
_URL_CREDENTIALS = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@")
_MAC_TEMP = re.compile(r"/(?:private/)?var/folders/[^\s\"']*?/T/")
_CODEX_NAME = re.compile(r"^codex(?:-bin|-code-mode-host)?$")
_COMPUTER_USE = re.compile(
    r"computer[\s_-]?use|node[\s_-]?repl|(?<![a-z])cua(?![a-z])|browser[\s_-]?use", re.I
)


@dataclass(frozen=True)
class ProvenanceCheck:
    name: str
    status: str
    detail: str
    summary: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Redaction


def redact(text: str) -> str:
    """Remove home paths, user directories, URL credentials, emails, user and host names."""
    home = str(Path.home())
    if len(home) > 1:
        text = text.replace(home, "~")
    text = _MAC_TEMP.sub("$TMPDIR/", text)
    text = _USER_DIR.sub("/<home>", text)
    # Temporary and project paths can embed the account name in other forms.
    for user in {Path.home().name, os.environ.get("USER", ""), os.environ.get("LOGNAME", "")}:
        if len(user) >= 3:
            text = re.sub(re.escape(user), "<user>", text, flags=re.I)
    text = _URL_CREDENTIALS.sub(r"\1<redacted>@", text)
    text = _EMAIL.sub("<email>", text)
    try:
        host = socket.gethostname().split(".")[0]
    except OSError:
        host = ""
    if len(host) >= 4:
        text = re.sub(re.escape(host), "<host>", text, flags=re.I)
    return text


def scrub(value: Any) -> Any:
    """Apply :func:`redact` to every string (keys included) in a JSON-like value."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {scrub(str(key)): scrub(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    return value


def _run(
    command: list[str], timeout: float = SUBPROCESS_TIMEOUT
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _plural(count: int, word: str) -> str:
    if count == 1:
        return f"{count} {word}"
    return f"{count} {word}{'es' if word.endswith(('s', 'x')) else 's'}"


# ---------------------------------------------------------------------------
# 1. install-provenance and 2. install-integrity


def _distribution() -> Distribution | None:
    try:
        return distribution("agentroute")
    except PackageNotFoundError:
        return None


def _file_url_path(url: str) -> str:
    parsed = urlparse(url)
    return unquote(parsed.path) if parsed.scheme == "file" else url


_UNSET: Any = object()


def install_provenance_check(
    dist: Distribution | None = _UNSET,
    module_dir: Path | None = None,
    manifest: dict[str, Any] | None = _UNSET,
) -> ProvenanceCheck:
    import agentroute

    dist = _distribution() if dist is _UNSET else dist
    module_dir = (module_dir or Path(agentroute.__file__).parent).resolve()
    manifest = read_manifest(agentroute_home()) if manifest is _UNSET else manifest
    summary: dict[str, Any] = {"manifest": bool(manifest)}
    if dist is None:
        summary.update(kind="unpackaged", version=getattr(agentroute, "__version__", "unknown"))
        return ProvenanceCheck(
            "install-provenance", "warn",
            f"no installed agentroute package metadata; importing {redact(str(module_dir))}",
            summary,
        )
    version = dist.version
    summary["version"] = version
    warnings: list[str] = []
    try:
        direct = json.loads(dist.read_text("direct_url.json") or "null")
    except ValueError:
        direct = None
    if not isinstance(direct, dict):
        kind, source = "index", "package index"
    elif isinstance(direct.get("dir_info"), dict):
        editable = bool(direct["dir_info"].get("editable"))
        kind = "editable" if editable else "local-directory"
        source = redact(_file_url_path(str(direct.get("url", ""))))
        warnings.append(f"{kind} install from {source}, not a release")
    elif isinstance(direct.get("vcs_info"), dict):
        kind = "vcs"
        commit = str(direct["vcs_info"].get("commit_id", ""))[:12]
        source = redact(str(direct.get("url", ""))) + (f"@{commit}" if commit else "")
        warnings.append(f"VCS install from {source}, not a release")
    else:
        url = str(direct.get("url", ""))
        name = Path(_file_url_path(url)).name
        kind = "local-wheel" if url.startswith("file:") else "archive"
        source = name or "archive"
    summary.update(kind=kind, source=source)

    if kind not in {"editable", "local-directory"}:
        try:
            installed_dir = Path(str(dist.locate_file("agentroute"))).resolve()
        except (OSError, TypeError):
            installed_dir = None
        if installed_dir is not None and installed_dir != module_dir:
            summary["imported_elsewhere"] = True
            warnings.append(
                f"code is imported from {redact(str(module_dir))}, not the installed package"
            )

    origin = ""
    if manifest:
        recorded = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
        source_kind = str(recorded.get("kind", "unknown"))
        ref = str(recorded.get("ref", "unknown"))
        summary["installer"] = {
            "kind": source_kind,
            "ref": ref if source_kind == "release" else ref[:12],
            "dirty": bool(recorded.get("dirty")),
        }
        origin = (
            f"; installed by release {ref}" if source_kind == "release"
            else f"; installed from source {ref[:12]}"
            + (" (dirty tree)" if recorded.get("dirty") else "")
        )
        if recorded.get("dirty"):
            warnings.append("source build came from a checkout with uncommitted changes")
        recorded_version = manifest.get("agentroute_version")
        if recorded_version and recorded_version != version:
            summary["version_matches_manifest"] = False
            warnings.append(
                f"package {version} differs from the {recorded_version} recorded at install time"
            )
        else:
            summary["version_matches_manifest"] = True
    else:
        origin = "; no install manifest"
    detail = f"agentroute {version} from {kind} ({source}){origin}"
    if warnings:
        return ProvenanceCheck(
            "install-provenance", "warn", detail + "; " + "; ".join(warnings), summary
        )
    return ProvenanceCheck("install-provenance", "pass", detail, summary)


def _record_name(path: str) -> str:
    parts = [part for part in Path(path).parts if part not in {"..", "."}]
    return "/".join(parts[-2:]) if parts and parts[0] == "bin" else "/".join(parts)


def install_integrity_check(dist: Distribution | None = _UNSET) -> ProvenanceCheck:
    dist = _distribution() if dist is _UNSET else dist
    if dist is None:
        return ProvenanceCheck(
            "install-integrity", "warn", "no installed package metadata to verify",
            {"verified": 0, "modified": [], "missing": []},
        )
    modified: list[str] = []
    missing: list[str] = []
    verified = 0
    # Parse RECORD directly: Distribution.files silently drops missing files.
    for row in csv.reader((dist.read_text("RECORD") or "").splitlines()):
        if len(row) < 2 or not row[1].startswith("sha256="):
            continue
        name = _record_name(row[0])
        path = Path(str(dist.locate_file(row[0])))
        try:
            digest = hashlib.sha256(path.read_bytes()).digest()
        except OSError:
            missing.append(name)
            continue
        verified += 1
        expected = row[1].removeprefix("sha256=")
        actual = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if actual != expected:
            modified.append(name)
    summary = {"verified": verified, "modified": sorted(modified), "missing": sorted(missing)}
    if modified or missing:
        parts = []
        if modified:
            parts.append(f"{len(modified)} modified: " + ", ".join(sorted(modified)[:10]))
        if missing:
            parts.append(f"{len(missing)} missing: " + ", ".join(sorted(missing)[:10]))
        return ProvenanceCheck(
            "install-integrity", "warn",
            "; ".join(parts) + "; reinstall with `agentroute update`", summary,
        )
    return ProvenanceCheck(
        "install-integrity", "pass",
        f"{_plural(verified, 'installed file')} match the package RECORD", summary,
    )


# ---------------------------------------------------------------------------
# 3. runtime-integrity


def runtime_integrity_check(
    home: Path, manifest: dict[str, Any] | None, build_id: str
) -> ProvenanceCheck:
    binaries = (
        manifest.get("binaries")
        if manifest and isinstance(manifest.get("binaries"), dict) else {}
    )
    hashes: dict[str, str] = {}
    for relative in sorted({*RUNTIME_FILES, *binaries}):
        try:
            hashes[relative] = sha256_file(home / relative)
        except OSError:
            continue
    if not manifest:
        return ProvenanceCheck(
            "runtime-integrity", "warn",
            "no install manifest; provenance unknown; reinstall with `agentroute update`",
            {"manifest": False, "binaries": hashes},
        )
    changed: list[str] = []
    for relative, expected in sorted(binaries.items()):
        if relative not in hashes:
            changed.append(f"{relative} missing")
        elif hashes[relative] != expected:
            changed.append(f"{relative} changed")
    summary: dict[str, Any] = {
        "manifest": True,
        "binaries": hashes,
        "changed": changed,
        "installed_at": manifest.get("installed_at"),
    }
    recorded_build = manifest.get("build_id")
    if recorded_build and recorded_build != build_id:
        changed.append(f"build-id {build_id} differs from installed {recorded_build}")
    if not binaries:
        return ProvenanceCheck(
            "runtime-integrity", "warn", "install manifest lists no runtime binaries", summary
        )
    if changed:
        return ProvenanceCheck(
            "runtime-integrity", "warn",
            "binary changed since install: " + "; ".join(changed)
            + "; reinstall with `agentroute update`",
            summary,
        )
    return ProvenanceCheck(
        "runtime-integrity", "pass",
        f"{_plural(len(hashes), 'runtime file')} match the install manifest "
        f"(installed {manifest.get('installed_at', 'unknown')})",
        summary,
    )


def install_time(home: Path, manifest: dict[str, Any] | None) -> float | None:
    if manifest and isinstance(manifest.get("installed_at"), str):
        try:
            return datetime.fromisoformat(manifest["installed_at"]).timestamp()
        except ValueError:
            pass
    try:
        return (home / "bin" / "codex-bin").stat().st_mtime
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 4. running-runtimes


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    exe: str
    # Used only for local classification; never reported because it can hold prompts.
    args: str
    age_seconds: float | None


def parse_etime(value: str) -> float | None:
    """Parse ps ``etime`` ([[dd-]hh:]mm:ss) into seconds."""
    try:
        days = 0
        if "-" in value:
            day_text, value = value.split("-", 1)
            days = int(day_text)
        parts = [int(part) for part in value.split(":")]
    except ValueError:
        return None
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts[-3:]
    return float(((days * 24 + hours) * 60 + minutes) * 60 + seconds)


def _ps_lines(fields: str) -> list[str] | None:
    result = _run(["ps", "-Ao", fields])
    if result is None or result.returncode != 0:
        return None
    return result.stdout.splitlines()


def list_processes(system: str | None = None) -> list[ProcessInfo] | None:
    """Return running processes, or None when the platform cannot list them."""
    system = system or platform.system()
    if system == "Darwin":
        # macOS reports the full executable path in ``comm``.
        lines = _ps_lines("pid=,etime=,comm=")
        if lines is None:
            return None
        args_lines = _ps_lines("pid=,args=") or []
        args: dict[int, str] = {}
        for line in args_lines:
            pid_text, _, rest = line.strip().partition(" ")
            if pid_text.isdigit():
                args[int(pid_text)] = rest.strip()
        processes = []
        for line in lines:
            fields = line.strip().split(None, 2)
            if len(fields) == 3 and fields[0].isdigit():
                pid = int(fields[0])
                processes.append(
                    ProcessInfo(pid, fields[2], args.get(pid, ""), parse_etime(fields[1]))
                )
        return processes
    if system == "Linux":
        proc = Path("/proc")
        if not proc.is_dir():
            return None
        lines = _ps_lines("pid=,etime=,args=")
        ages: dict[int, float | None] = {}
        args = {}
        for line in lines or []:
            fields = line.strip().split(None, 2)
            if len(fields) >= 2 and fields[0].isdigit():
                ages[int(fields[0])] = parse_etime(fields[1])
                args[int(fields[0])] = fields[2] if len(fields) == 3 else ""
        processes = []
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                exe = os.readlink(entry / "exe")
            except OSError:
                continue  # other users' processes are not readable and not relevant
            command = args.get(pid)
            if command is None:
                try:
                    raw = (entry / "cmdline").read_bytes()
                    command = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
                except OSError:
                    command = ""
            processes.append(ProcessInfo(pid, exe, command, ages.get(pid)))
        return processes
    return None


_AGENTROUTE_MODULE = re.compile(r"(?:^|\s)-m\s+agentroute(?:[.\s]|$)")


def _is_agentroute_service(name: str, args: str) -> bool:
    """A Python or entry-point process running AgentRoute code (bridge, daemons, proxies)."""
    if not (name.startswith("python") or name == "agentroute"):
        return False
    if _AGENTROUTE_MODULE.search(args):
        return True
    return any(Path(token).name == "agentroute" for token in args.split()[:3])


def _inside_app(path: str) -> bool:
    return ".app/" in path or path.endswith(".app")


def running_runtimes_check(
    home: Path,
    since_install: float | None,
    processes: list[ProcessInfo] | None,
    routed_desktop: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
    now: float | None = None,
) -> ProvenanceCheck:
    launcher = home / "bin" / "codex"
    resolved_path_codex = which("codex")
    path_note = ""
    path_warning = ""
    if resolved_path_codex is None:
        path_note = "codex is not on PATH"
    else:
        try:
            same = Path(resolved_path_codex).resolve() == launcher.resolve()
        except OSError:
            same = False
        if same:
            path_note = "codex on PATH is the AgentRoute launcher"
        else:
            path_warning = (
                f"codex on PATH resolves to {redact(resolved_path_codex)}, "
                "not the AgentRoute launcher"
            )
    summary: dict[str, Any] = {"path_codex_is_launcher": bool(path_note.endswith("launcher"))}
    if processes is None:
        detail = "process listing unavailable on this system"
        if path_warning:
            return ProvenanceCheck("running-runtimes", "warn", f"{detail}; {path_warning}", summary)
        return ProvenanceCheck("running-runtimes", "pass", f"{detail}; {path_note}", summary)

    now = time.time() if now is None else now
    installed = str((home / "bin" / "codex-bin").resolve())
    installed_helpers = {str((home / "bin" / "codex-code-mode-host").resolve())}
    desktop_binary = (
        str(routed_desktop / "Contents" / "Resources" / "codex-bin")
        if routed_desktop is not None else None
    )
    own = {os.getpid(), os.getppid()}
    installed_count = installed_stale = 0
    desktop_count = 0
    others: Counter[str] = Counter()
    suspicious: Counter[str] = Counter()
    services = services_stale = 0

    def predates_install(process: ProcessInfo) -> bool:
        return (
            since_install is not None
            and process.age_seconds is not None
            and now - process.age_seconds < since_install - 1
        )

    for process in processes:
        if process.pid in own:
            continue
        exe = process.exe
        replaced = exe.endswith(" (deleted)")
        exe = exe.removesuffix(" (deleted)")
        name = Path(exe).name
        if _CODEX_NAME.match(name):
            if name == "codex" and not _inside_app(exe) and exe == str(launcher):
                continue  # the shell launcher itself before it execs the runtime
            if exe == installed or exe in installed_helpers:
                installed_count += 1
                if replaced or predates_install(process):
                    installed_stale += 1
            elif desktop_binary and exe == desktop_binary:
                desktop_count += 1
            else:
                label = redact(exe)
                others[label] += 1
                private_copy = (
                    name in {"codex-bin", "codex-code-mode-host"}
                    and not _inside_app(exe) and "/node_modules/" not in exe
                )
                if private_copy or "/target/" in exe or replaced:
                    suspicious[label] += 1
            continue
        if _is_agentroute_service(name, process.args):
            services += 1
            if replaced or predates_install(process):
                services_stale += 1

    summary.update(
        installed=installed_count,
        installed_stale=installed_stale,
        routed_desktop=desktop_count,
        other_runtimes=dict(sorted(others.items())),
        unexpected_runtimes=dict(sorted(suspicious.items())),
        services=services,
        services_stale=services_stale,
    )
    parts = [f"installed runtime: {installed_count} running"]
    if desktop_count:
        parts.append(f"routed Desktop runtime: {desktop_count}")
    if others:
        parts.append(
            "other codex binaries: "
            + ", ".join(f"{label} x{count}" for label, count in sorted(others.items()))
        )
    parts.append(f"AgentRoute services: {services}")
    if path_note:
        parts.append(path_note)
    warnings = []
    if installed_stale:
        warnings.append(
            f"{_plural(installed_stale, 'process')} still on the previous build; restart them"
        )
    if services_stale:
        warnings.append(
            f"{_plural(services_stale, 'AgentRoute service')} started before the current "
            "install; restart them"
        )
    if suspicious:
        warnings.append(
            f"{_plural(sum(suspicious.values()), 'process')} run a private or development "
            "runtime instead of the installed one (check terminal multiplexer and shell PATH)"
        )
    if path_warning:
        warnings.append(path_warning)
    detail = "; ".join(parts)
    if warnings:
        return ProvenanceCheck(
            "running-runtimes", "warn", detail + "; " + "; ".join(warnings), summary
        )
    return ProvenanceCheck("running-runtimes", "pass", detail, summary)


# ---------------------------------------------------------------------------
# 5. shared-server


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def shared_server_check(config: AppConfig, home: Path) -> ProvenanceCheck:
    from . import shared_server

    try:
        metadata = shared_server._metadata()
    except Exception:  # noqa: BLE001 - diagnostics must not fail doctor
        metadata = {}
    enabled = bool(getattr(config.shared_server, "enabled", False))
    if not metadata or not _pid_alive(metadata.get("pid")):
        return ProvenanceCheck(
            "shared-server", "pass",
            "not running" + ("" if enabled else "; shared server disabled"),
            {"running": False, "enabled": enabled},
        )
    summary: dict[str, Any] = {"running": True, "enabled": enabled}
    binary = home / "bin" / "codex-bin"
    try:
        expected = shared_server._fingerprint(
            binary, config, shared_server.server_environment(prune=False)
        )
    except Exception as error:  # noqa: BLE001
        summary["fingerprint_match"] = None
        return ProvenanceCheck(
            "shared-server", "warn",
            f"running; cannot compute the current settings fingerprint ({type(error).__name__})",
            summary,
        )
    recorded_binary = str(metadata.get("binary", ""))
    try:
        binary_matches = recorded_binary == str(binary.resolve())
    except OSError:
        binary_matches = False
    matches = metadata.get("fingerprint") == expected
    summary.update(fingerprint_match=matches, binary_match=binary_matches)
    if matches and binary_matches:
        return ProvenanceCheck(
            "shared-server", "pass",
            "running; settings fingerprint matches the installed runtime", summary,
        )
    reason = (
        "started from a different runtime binary" if not binary_matches
        else "settings fingerprint no longer matches the installed runtime/settings"
    )
    return ProvenanceCheck(
        "shared-server", "warn",
        f"running but {reason}; new terminals cannot attach until it restarts. Close attached "
        "clients, run `agentroute server stop`, then start it again and relaunch clients",
        summary,
    )


# ---------------------------------------------------------------------------
# 6. computer-use


@dataclass(frozen=True)
class Signature:
    kind: str  # developer-id, apple, development, adhoc, unsigned, other, unknown
    team: str | None


def parse_codesign(output: str, returncode: int) -> Signature:
    if "not signed at all" in output:
        return Signature("unsigned", None)
    team = None
    match = re.search(r"^TeamIdentifier=(.+)$", output, re.M)
    if match and match.group(1).strip() != "not set":
        team = match.group(1).strip()
    if re.search(r"^Signature=adhoc", output, re.M) or "flags=0x2(adhoc)" in output:
        return Signature("adhoc", team)
    if returncode != 0:
        return Signature("unknown", team)
    if "Authority=Developer ID Application" in output:
        return Signature("developer-id", team)
    if "Authority=Apple Development" in output:
        return Signature("development", team)
    if "Authority=Software Signing" in output or "Authority=Apple Mac OS Application" in output:
        return Signature("apple", team)
    return Signature("other", team)


def codesign_signature(path: Path) -> Signature | None:
    """Read a code signature with ``codesign``; None when the tool is unavailable."""
    if shutil.which("codesign") is None:
        return None
    result = _run(["codesign", "-dv", "--verbose=2", str(path)])
    if result is None:
        return Signature("unknown", None)
    return parse_codesign(result.stderr + result.stdout, result.returncode)


def _expand(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path)


def _looks_like_path(value: str) -> bool:
    return (
        (value.startswith("/") or value.startswith("~") or value.startswith("./"))
        and os.pathsep not in value
    )


def computer_use_references(
    settings: dict[str, Any], config_dir: Path
) -> tuple[list[tuple[str, Path, bool]], list[str]]:
    """Return (label, path, must_be_executable) references and the disabled server names."""
    references: list[tuple[str, Path, bool]] = []
    disabled: list[str] = []
    servers = settings.get("mcp_servers")
    for name, server in (servers.items() if isinstance(servers, dict) else ()):
        if not isinstance(server, dict):
            continue
        command = server.get("command") if isinstance(server.get("command"), str) else ""
        env = server.get("env") if isinstance(server.get("env"), dict) else {}
        blob = " ".join([str(name), command, *map(str, env.keys()), *map(str, env.values())])
        if not _COMPUTER_USE.search(blob):
            continue
        if server.get("enabled") is False:
            disabled.append(str(name))
            continue
        cwd = server.get("cwd")
        base = _expand(cwd, config_dir) if isinstance(cwd, str) else config_dir
        if command:
            if "/" in command or command.startswith("~"):
                references.append((f"{name}.command", _expand(command, base), True))
            elif (found := shutil.which(command)) is not None:
                references.append((f"{name}.command", Path(found), True))
        for key, value in env.items():
            if not isinstance(value, str):
                continue
            if "TRUSTED_SERVICES" in key:
                try:
                    services = json.loads(value)
                except ValueError:
                    services = {}
                for service, target in (services.items() if isinstance(services, dict) else ()):
                    if isinstance(target, str) and _looks_like_path(target):
                        references.append(
                            (f"{name}.{key}.{service}", _expand(target, base), False)
                        )
            elif key.endswith("_PATH") and _looks_like_path(value):
                must_execute = key.endswith(("_NODE_PATH", "_CLI_PATH", "_BIN_PATH"))
                references.append((f"{name}.{key}", _expand(value, base), must_execute))
    notify = settings.get("notify")
    if (
        isinstance(notify, list) and notify and isinstance(notify[0], str)
        and _COMPUTER_USE.search(notify[0]) and _looks_like_path(notify[0])
    ):
        references.append(("notify", _expand(notify[0], config_dir), True))
    unique: dict[Path, tuple[str, Path, bool]] = {}
    for label, path, executable in references:
        if path not in unique or executable:
            unique[path] = (label, path, executable or unique.get(path, ("", path, False))[2])
    return list(unique.values())[:MAX_COMPUTER_USE_PATHS], disabled


def _outermost_app(path: Path) -> Path | None:
    outermost = None
    for parent in path.parents:
        if parent.suffix == ".app":
            outermost = parent
    return outermost


def _codex_settings() -> tuple[dict[str, Any], Path]:
    config_file = codex_home() / "config.toml"
    try:
        return tomllib.loads(config_file.read_text(encoding="utf-8")), config_file.parent
    except (OSError, ValueError):
        return {}, config_file.parent


def computer_use_check(
    settings: dict[str, Any] | None = None,
    config_dir: Path | None = None,
    system: str | None = None,
    signature: Callable[[Path], Signature | None] = codesign_signature,
    routed_desktop: Path | None = None,
) -> ProvenanceCheck:
    if settings is None:
        settings, config_dir = _codex_settings()
    config_dir = config_dir or codex_home()
    system = system or platform.system()
    references, disabled = computer_use_references(settings, config_dir)
    if not references:
        detail = "not configured"
        if disabled:
            detail += f" ({_plural(len(disabled), 'disabled server')})"
        return ProvenanceCheck("computer-use", "pass", detail, {"configured": False, "paths": []})

    cache: dict[Path, Signature | None] = {}

    def signature_of(path: Path) -> Signature | None:
        if path not in cache:
            cache[path] = signature(path)
        return cache[path]

    problems: list[str] = []
    entries: list[dict[str, Any]] = []
    signing_available = system == "Darwin"
    tool_missing = False
    for label, path, must_execute in references:
        shown = redact(str(path))
        entry: dict[str, Any] = {"ref": label, "path": shown, "exists": path.exists()}
        entries.append(entry)
        if not path.exists():
            problems.append(f"{label}: missing {shown}")
            continue
        is_app = path.is_dir() and path.suffix == ".app"
        executable = path.is_file() and os.access(path, os.X_OK)
        entry["executable"] = executable
        if must_execute and not (executable or is_app):
            problems.append(f"{label}: not executable {shown}")
        try:
            inside_routed = routed_desktop is not None and path.resolve().is_relative_to(
                routed_desktop.resolve()
            )
        except OSError:
            inside_routed = False
        if inside_routed:
            problems.append(
                f"{label}: points inside the AgentRoute-rebuilt Desktop app (re-signed copy); "
                "use the vendor-installed app"
            )
        if not signing_available or not (is_app or executable):
            continue
        own = signature_of(path)
        if own is None:
            tool_missing = True
            continue
        entry["signature"] = own.kind
        container = _outermost_app(path)
        if container is not None:
            outer = signature_of(container)
            if outer is not None and outer.team:
                match = own.team == outer.team
                entry["team_matches_app"] = match
                if not match:
                    problems.append(
                        f"{label}: {own.kind} signature does not carry the Team ID of its "
                        f"enclosing app {redact(str(container))}"
                    )
                    continue
        if own.kind in {"adhoc", "unsigned"}:
            problems.append(f"{label}: {own.kind} signature on {shown}")
    summary = {
        "configured": True,
        "paths": entries,
        "signature_checks": "skipped" if not signing_available else
        "unavailable" if tool_missing else "done",
    }
    kinds = Counter(entry.get("signature", "n/a") for entry in entries)
    detail = f"{_plural(len(entries), 'referenced path')}"
    if signing_available and not tool_missing:
        detail += "; signatures: " + ", ".join(
            f"{kind} x{count}" for kind, count in sorted(kinds.items())
        )
    elif signing_available:
        detail += "; codesign unavailable, signatures not checked"
    else:
        detail += f"; signature checks skipped on {system.lower()}"
    if problems:
        return ProvenanceCheck("computer-use", "warn", detail + "; " + "; ".join(problems), summary)
    return ProvenanceCheck("computer-use", "pass", detail, summary)


# ---------------------------------------------------------------------------
# 7. accounts


def accounts_check(config: AppConfig, now: float | None = None) -> ProvenanceCheck:
    from .profiles import account_credential_home, profile_account_hash

    now = time.time() if now is None else now
    profiles = {
        name: profile for name, profile in config.capacity.profiles.items() if profile.enabled
    }
    if not profiles:
        return ProvenanceCheck(
            "accounts", "pass", "no account profiles configured",
            {"profiles": 0, "duplicates": False},
        )
    by_hash: dict[str, list[str]] = {}
    missing: list[str] = []
    stale: list[str] = []
    for name, profile in sorted(profiles.items()):
        auth_path = account_credential_home(name, profile) / "auth.json"
        try:
            payload = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            missing.append(name)
            continue
        if not isinstance(payload, dict):
            missing.append(name)
            continue
        tokens = payload.get("tokens")
        if not isinstance(tokens, dict) and not payload.get("OPENAI_API_KEY"):
            missing.append(name)
            continue
        refreshed = payload.get("last_refresh")
        if isinstance(refreshed, str):
            try:
                stamp = datetime.fromisoformat(refreshed.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                days = (now - stamp.timestamp()) / 86400
                if days > STALE_LOGIN_DAYS:
                    stale.append(f"{name} ({int(days)}d)")
            except ValueError:
                pass
        account = profile_account_hash(name, profile)
        if account:
            by_hash.setdefault(account, []).append(name)
    duplicates = [names for names in by_hash.values() if len(names) > 1]
    summary = {
        "profiles": len(profiles),
        "duplicates": bool(duplicates),
        "missing": len(missing),
        "stale": len(stale),
    }
    problems = []
    if duplicates:
        problems.append(
            "same account signed in under several profiles: "
            + "; ".join(" = ".join(names) for names in duplicates)
            + " (failover between them cannot add capacity)"
        )
    if missing:
        problems.append("no stored login: " + ", ".join(missing))
    if stale:
        problems.append(
            f"login not refreshed in over {STALE_LOGIN_DAYS} days: " + ", ".join(stale)
        )
    detail = f"{_plural(len(profiles), 'enabled profile')}; {len(by_hash)} distinct account(s)"
    if problems:
        return ProvenanceCheck(
            "accounts", "warn", redact(detail + "; " + "; ".join(problems)), summary
        )
    return ProvenanceCheck("accounts", "pass", detail, summary)


# ---------------------------------------------------------------------------
# 8. compaction


def runtime_revision(build_id: str) -> int | None:
    match = _REVISION.search(build_id)
    return int(match.group(1)) if match else None


def compaction_check(build_id: str) -> ProvenanceCheck:
    revision = runtime_revision(build_id)
    summary = {"revision": revision, "minimum": MIN_PORTABLE_COMPACTION_REVISION}
    if revision is None:
        return ProvenanceCheck(
            "compaction", "warn",
            f"runtime revision unknown; portable cross-provider compaction needs "
            f"v{MIN_PORTABLE_COMPACTION_REVISION} or newer",
            summary,
        )
    if revision < MIN_PORTABLE_COMPACTION_REVISION:
        return ProvenanceCheck(
            "compaction", "warn",
            f"runtime v{revision} predates portable cross-provider compaction "
            f"(v{MIN_PORTABLE_COMPACTION_REVISION}); run `agentroute update`",
            summary,
        )
    return ProvenanceCheck(
        "compaction", "pass",
        f"runtime v{revision} provides portable cross-provider compaction "
        f"(requires v{MIN_PORTABLE_COMPACTION_REVISION}+)",
        summary,
    )


# ---------------------------------------------------------------------------
# Orchestration


def _routed_desktop() -> Path | None:
    if platform.system() != "Darwin":
        return None
    from .desktop import DEFAULT_DESTINATION_APP

    return DEFAULT_DESTINATION_APP


def _guard(name: str, function: Callable[[], ProvenanceCheck]) -> ProvenanceCheck:
    try:
        return function()
    except Exception as error:  # noqa: BLE001 - a diagnostic must never break doctor
        return ProvenanceCheck(name, "warn", f"check could not run ({type(error).__name__})")


def provenance_checks(config: AppConfig, build_id: str) -> list[ProvenanceCheck]:
    """Run the setup-provenance checks. They warn at most; they never fail doctor."""
    home = agentroute_home()
    manifest = read_manifest(home)
    dist = _distribution()
    routed = _routed_desktop()
    return [
        _guard("install-provenance", lambda: install_provenance_check(dist, None, manifest)),
        _guard("install-integrity", lambda: install_integrity_check(dist)),
        _guard("runtime-integrity", lambda: runtime_integrity_check(home, manifest, build_id)),
        _guard("running-runtimes", lambda: running_runtimes_check(
            home, install_time(home, manifest), list_processes(), routed,
        )),
        _guard("shared-server", lambda: shared_server_check(config, home)),
        _guard("computer-use", lambda: computer_use_check(routed_desktop=routed)),
        _guard("accounts", lambda: accounts_check(config)),
        _guard("compaction", lambda: compaction_check(build_id)),
    ]


# ---------------------------------------------------------------------------
# Fingerprint and compare


def config_shape(config: AppConfig) -> dict[str, Any]:
    """Describe configuration structure without credentials, URLs, or account data."""
    backends = {}
    for name, backend in sorted(config.backends.items()):
        backends[name] = {
            "enabled": backend.enabled,
            "provider": backend.codex_provider,
            "tiers": {tier: target.model for tier, target in sorted(backend.tiers.items())},
            "tool_compatibility": backend.tool_compatibility,
            "fallback": backend.fallback_backend,
        }
    classifier = config.routing.classifier
    return {
        "routing_enabled": config.enabled,
        "backends": backends,
        "classifier": {
            "enabled": classifier.enabled,
            "engine": classifier.engine,
            "transport": classifier.transport,
            "model": classifier.model,
        },
        "capacity_enabled": config.capacity.enabled,
        "profile_count": sum(1 for profile in config.capacity.profiles.values() if profile.enabled),
        "shared_server_enabled": bool(getattr(config.shared_server, "enabled", False)),
        "max_tier": str(config.policy.max_tier),
    }


def build_fingerprint(
    config: AppConfig,
    checks: Iterable[Any],
    provenance: Iterable[ProvenanceCheck],
    build_id: str,
) -> dict[str, Any]:
    by_name = {check.name: check for check in provenance}

    def summary(name: str) -> dict[str, Any]:
        check = by_name.get(name)
        return dict(check.summary) if check else {}

    accounts = summary("accounts")
    shared = summary("shared-server")
    snapshot = {
        "schema": FINGERPRINT_SCHEMA,
        "platform": {
            "system": platform.system().lower(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": ".".join(map(str, sys.version_info[:3])),
        },
        "agentroute": summary("install-provenance"),
        "runtime": {
            "build_id": build_id,
            "revision": runtime_revision(build_id),
            **summary("runtime-integrity"),
        },
        "integrity": summary("install-integrity"),
        "running": summary("running-runtimes"),
        "shared_server": {
            key: shared.get(key)
            for key in ("enabled", "running", "fingerprint_match", "binary_match")
            if key in shared
        },
        "config": config_shape(config),
        "accounts": {
            "profiles": accounts.get("profiles", 0),
            "duplicates": accounts.get("duplicates", False),
            "missing": accounts.get("missing", 0),
            "stale": accounts.get("stale", 0),
        },
        "computer_use": summary("computer-use"),
        "checks": {check.name: check.status for check in checks},
    }
    return scrub(snapshot)


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict) and value:
        flat: dict[str, Any] = {}
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    return {prefix: value}


_SECTION_LABELS = {
    "platform": "platform",
    "agentroute": "agentroute install",
    "runtime": "runtime",
    "integrity": "package integrity",
    "running": "running processes",
    "shared_server": "shared server",
    "config": "configuration",
    "accounts": "accounts",
    "computer_use": "computer use",
    "checks": "doctor checks",
}


def compare_fingerprints(theirs: dict[str, Any], ours: dict[str, Any]) -> list[dict[str, Any]]:
    """Return differences as {section, key, theirs, ours}, ignoring identical values."""
    differences = []
    sections = list(dict.fromkeys([*_SECTION_LABELS, *theirs, *ours]))
    for section in sections:
        if section == "schema":
            continue
        left = _flatten(theirs.get(section), "")
        right = _flatten(ours.get(section), "")
        for key in sorted(set(left) | set(right)):
            if left.get(key) != right.get(key):
                differences.append({
                    "section": section,
                    "key": key,
                    "theirs": left.get(key),
                    "ours": right.get(key),
                })
    return differences


def _show(value: Any) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, list):
        return ", ".join(map(str, value)) if value else "(none)"
    return str(value)


def format_differences(differences: list[dict[str, Any]], schemas: tuple[Any, Any]) -> str:
    lines = []
    if schemas[0] != schemas[1]:
        lines.append(f"fingerprint schema: {schemas[0]} vs {schemas[1]}")
    if not differences:
        lines.append("No differences.")
        return "\n".join(lines)
    current = None
    for item in differences:
        if item["section"] != current:
            current = item["section"]
            lines.append(f"{_SECTION_LABELS.get(current, current)}:")
        key = item["key"] or current
        lines.append(f"  {key}: {_show(item['theirs'])} (theirs) vs {_show(item['ours'])} (ours)")
    return "\n".join(lines)
