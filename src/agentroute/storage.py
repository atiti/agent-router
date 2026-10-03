"""Retention for expendable AgentRoute diagnostics; never touches Codex chats."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from .config import agentroute_home

TRACE_LIMIT = 512 * 1024 * 1024
BRIDGE_LIMIT = 64 * 1024 * 1024


def file_sizes(root: Path) -> tuple[int, float]:
    size, newest = 0, 0.0
    if root.is_symlink():
        return size, newest
    paths = root.rglob("*") if root.is_dir() else [root]
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            info = path.stat()
            size += info.st_size
            newest = max(newest, info.st_mtime)
        except OSError:
            continue
    return size, newest


def prune_group(paths: list[Path], limit: int, days: int, *, now: float) -> dict[str, int]:
    entries = sorted(
        ((file_sizes(path), path) for path in paths if not path.is_symlink()),
        key=lambda entry: entry[0][1],
    )
    remaining = sum(info[0] for info, _ in entries)
    removed = 0
    for (size, newest), path in entries:
        if newest > now - 3600:  # Never remove a bundle with recent writes.
            continue
        if newest >= now - days * 86400 and remaining <= limit:
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        remaining -= size
        removed += size
    return {"removed_bytes": removed, "remaining_bytes": remaining}


def prune_diagnostics(root: Path | None = None) -> dict[str, dict[str, int]]:
    root = root or agentroute_home() / "profiling"
    if root.is_symlink() or any((root / name).is_symlink() for name in ("traces", "bridge")):
        raise ValueError("refusing a symlinked profiling directory")
    now = time.time()
    traces = root / "traces"
    # Native writers reserve bytes using this same lock. Rescan after removal so
    # the quota remains conservative across crashes and concurrent processes.
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    traces.mkdir(parents=True, exist_ok=True, mode=0o700)
    traces.chmod(0o700)
    import fcntl

    with (traces / ".budget").open("a+b") as budget:
        (traces / ".budget").chmod(0o600)
        fcntl.flock(budget, fcntl.LOCK_EX)
        result = {"traces": prune_group(list(traces.glob("trace-*")), TRACE_LIMIT, 7, now=now)}
        used, _ = file_sizes(traces)
        stored = (traces / ".budget").stat().st_size
        budget.seek(0)
        budget.truncate()
        budget.write(max(0, used - stored).to_bytes(8, "little"))
        budget.flush()
    result["bridge"] = prune_group(
        list((root / "bridge").glob("*/*.json")), BRIDGE_LIMIT, 30, now=now
    )
    return result


def prune_desktop_backups(home: Path | None = None) -> int:
    root = (home or agentroute_home()) / "backups" / "desktop"
    if root.is_symlink():
        raise ValueError("refusing a symlinked backup directory")
    backups = sorted(
        (p for p in root.glob("ChatGPT-Routed-*.app") if p.is_dir() and not p.is_symlink()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for path in backups[2:]:
        size, _ = file_sizes(path)
        shutil.rmtree(path)
        removed += size
    return removed


def prune_build_cache(home: Path | None = None) -> int:
    """Remove only known Rust output paths, and only while no Rust build is active."""
    home = home or agentroute_home()
    processes = subprocess.run(
        ["ps", "-axo", "comm="], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    if any(
        Path(command.strip()).name in {"cargo", "rustc", "cargo-nextest"} for command in processes
    ):
        raise RuntimeError("Rust build is active; retry build-cache cleanup after it finishes")
    removed = 0
    for relative in ("build/codex", "build/codex-0155", "src/codex/codex-rs/target"):
        path = home / relative
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError(f"refusing a symlinked build path: {path}")
        if path.is_dir():
            # Rust outputs are recognizable. Unknown directories are not caches.
            if not (path / ".rustc_info.json").is_file() and not (path / "CACHEDIR.TAG").is_file():
                continue
            size, _ = file_sizes(path)
            shutil.rmtree(path)
            removed += size
    return removed


def prepare_capture(environment: dict[str, str]) -> None:
    from .context_profile import capture_enabled, profile_root

    periodic_prune()
    if capture_enabled() and not environment.get("CODEX_ROLLOUT_TRACE_ROOT"):
        environment["CODEX_ROLLOUT_TRACE_ROOT"] = str(profile_root() / "traces")
    if environment.get("CODEX_ROLLOUT_TRACE_ROOT"):
        environment["CODEX_ROLLOUT_TRACE_MAX_BYTES"] = str(TRACE_LIMIT)
    # Native writers observe removal of this marker during an existing session.
    if (
        environment.get("CODEX_ROLLOUT_TRACE_ROOT") == str(profile_root() / "traces")
        and environment.get("AGENTROUTE_PROFILE_CAPTURE") != "1"
    ):
        environment["CODEX_ROLLOUT_TRACE_ENABLED_FILE"] = str(profile_root() / "enabled")


_last_prune = 0.0


def periodic_prune() -> None:
    global _last_prune
    now = time.monotonic()
    if now - _last_prune < 300:
        return
    _last_prune = now
    try:
        prune_diagnostics()
    except (OSError, ValueError):
        pass  # Diagnostics must never prevent a provider request.
