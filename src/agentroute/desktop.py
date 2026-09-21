from __future__ import annotations

import platform
import plistlib
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .code_mode_smoke import smoke_code_mode_host
from .config import agentroute_home

DEFAULT_SOURCE_APP = Path("/Applications/ChatGPT.app")
DEFAULT_DESTINATION_APP = Path("/Applications/ChatGPT-Routed.app")


def _assets_dir() -> Path:
    return Path(__file__).with_name("desktop_assets")


def _run(*command: str | Path, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(part) for part in command],
        check=True,
        capture_output=capture,
        text=True,
    )


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise RuntimeError("Codex Desktop installation is supported only on macOS")


@contextmanager
def _desktop_build_lock(home: Path) -> Iterator[None]:
    import fcntl

    home.mkdir(parents=True, exist_ok=True)
    lock_path = home / "desktop-rebuild.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(
                "another AgentRoute Desktop build is already running; wait for it to finish"
            ) from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_build_id(home: Path) -> str:
    path = home / "build-id"
    return path.read_text(encoding="utf-8").splitlines()[0] if path.exists() else "unknown"


def _codex_version(binary: Path) -> str | None:
    if not binary.exists():
        return None
    try:
        result = _run(binary, "--version", capture=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return (result.stdout or result.stderr).strip() or None


def desktop_status(
    source: Path = DEFAULT_SOURCE_APP,
    destination: Path = DEFAULT_DESTINATION_APP,
) -> dict[str, str | bool | None]:
    home = agentroute_home()
    source_version = _codex_version(source / "Contents/Resources/codex")
    routed_version = _codex_version(home / "bin/codex-bin")
    return {
        "source_exists": source.is_dir(),
        "destination_exists": destination.is_dir(),
        "source_version": source_version,
        "destination_version": _codex_version(destination / "Contents/Resources/codex"),
        "routed_binary_version": routed_version,
        "source_routed_versions_match": bool(
            source_version and routed_version and source_version == routed_version
        ),
        "build_id": _read_build_id(home),
    }


def build_desktop_app(
    source: Path = DEFAULT_SOURCE_APP,
    destination: Path = DEFAULT_DESTINATION_APP,
    *,
    signing_identity: str = "-",
    replace: bool = False,
    allow_version_mismatch: bool = False,
) -> tuple[Path, Path | None]:
    """Derive a signed routed app from the user's installed official app."""
    _require_macos()
    home = agentroute_home()
    with _desktop_build_lock(home):
        return _build_desktop_app_locked(
            source,
            destination,
            signing_identity=signing_identity,
            replace=replace,
            allow_version_mismatch=allow_version_mismatch,
        )


def _build_desktop_app_locked(
    source: Path,
    destination: Path,
    *,
    signing_identity: str,
    replace: bool,
    allow_version_mismatch: bool,
) -> tuple[Path, Path | None]:
    home = agentroute_home()
    codex = home / "bin/codex-bin"
    code_mode_host = home / "bin/codex-code-mode-host"
    launcher = _assets_dir() / "codex-launcher"
    entitlements = _assets_dir() / "ChatGPT-Routed.entitlements.plist"
    code_mode_entitlements = _assets_dir() / "codex-code-mode-host.entitlements.plist"

    if not source.is_dir():
        raise FileNotFoundError(f"official source app does not exist: {source}")
    for required in (codex, code_mode_host, launcher, entitlements, code_mode_entitlements):
        if not required.exists():
            raise FileNotFoundError(f"required AgentRoute asset does not exist: {required}")
    source_version = _codex_version(source / "Contents/Resources/codex")
    routed_version = _codex_version(codex)
    if not allow_version_mismatch and source_version != routed_version:
        raise RuntimeError(
            "official and routed Codex versions differ "
            f"({source_version or 'unknown'} != {routed_version or 'unknown'}); "
            "update AgentRoute first or explicitly allow the mismatch"
        )
    if destination.exists() and not replace:
        raise FileExistsError(f"destination exists; use desktop rebuild: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=".ChatGPT-Routed.build.", dir=destination.parent)
    )
    staging_app = staging_root / destination.name
    backup: Path | None = None
    try:
        if destination.exists():
            backup_root = home / "backups" / "desktop"
            backup_root.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = backup_root / f"{destination.stem}-{timestamp}.app"
            shutil.move(destination, backup)
        _run("ditto", source, staging_app)
        resources = staging_app / "Contents/Resources"
        info_path = staging_app / "Contents/Info.plist"
        shutil.copy2(codex, resources / "codex-bin")
        shutil.copy2(code_mode_host, resources / "codex-code-mode-host")
        shutil.copy2(launcher, resources / "codex")
        for executable in ("codex", "codex-bin", "codex-code-mode-host"):
            (resources / executable).chmod(0o755)

        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
        info.update(
            {
                "CFBundleDisplayName": "ChatGPT-Routed",
                "CFBundleName": "ChatGPT-Routed",
                "LSHasLocalizedDisplayName": False,
                "SUEnableAutomaticChecks": False,
                "SUAutomaticallyUpdate": False,
                "AgentRouteDesktopBuild": _read_build_id(home),
            }
        )
        with info_path.open("wb") as handle:
            plistlib.dump(info, handle, sort_keys=False)

        _run("codesign", "--force", "--deep", "--sign", signing_identity, resources / "codex-bin")
        _run(
            "codesign",
            "--force",
            "--deep",
            "--sign",
            signing_identity,
            "--entitlements",
            code_mode_entitlements,
            resources / "codex-code-mode-host",
        )
        _run(
            "codesign",
            "--force",
            "--deep",
            "--sign",
            signing_identity,
            "--options",
            "runtime",
            "--entitlements",
            entitlements,
            staging_app,
        )
        _run("codesign", "--verify", "--deep", "--strict", "--verbose=2", staging_app)
        smoke_code_mode_host(resources / "codex-code-mode-host")
        _run(resources / "codex", "app-server", "--help", capture=True)
        if destination.exists():
            conflict_root = home / "backups" / "desktop" / "conflicts"
            conflict_root.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            conflict = conflict_root / f"{destination.stem}-{timestamp}.app"
            shutil.move(destination, conflict)
            if backup is not None and backup.exists():
                shutil.move(backup, destination)
            raise FileExistsError(
                "Desktop destination appeared during rebuild; restored the previous app and "
                f"preserved the conflicting bundle at {conflict}"
            )
        shutil.move(staging_app, destination)
    except Exception:
        if backup is not None and backup.exists() and not destination.exists():
            shutil.move(backup, destination)
        raise
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return destination, backup


def rollback_desktop_app(destination: Path = DEFAULT_DESTINATION_APP) -> tuple[Path, Path]:
    """Restore the newest routed-app backup and retain the replaced app."""
    _require_macos()
    backup_root = agentroute_home() / "backups" / "desktop"
    backups = sorted(
        (
            path
            for path in backup_root.glob(f"{destination.stem}-*.app")
            if "-rollback-replaced-" not in path.name
        ),
        reverse=True,
    )
    if not backups:
        raise FileNotFoundError("no routed Desktop backup is available")
    selected = backups[0]
    replaced = backup_root / (
        f"{destination.stem}-rollback-replaced-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.app"
    )
    if destination.exists():
        shutil.move(destination, replaced)
    try:
        shutil.move(selected, destination)
    except Exception:
        if replaced.exists() and not destination.exists():
            shutil.move(replaced, destination)
        raise
    return destination, replaced
