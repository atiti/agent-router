from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

PINNED_CODEX_COMMIT = "b412ff32c417f855c2b2d1581b77058eed87c84b"


def patch_paths() -> tuple[Path, ...]:
    patch_dir = Path(__file__).with_name("patches")
    return (
        patch_dir / "codex-user-prompt-model-override.patch",
        patch_dir / "codex-package-version.patch",
        patch_dir / "codex-history-recovery.patch",
        patch_dir / "codex-route-application-receipt.patch",
    )


def patch_path() -> Path:
    """Return the primary patch path for backwards compatibility."""
    return patch_paths()[0]


def validate_codex_source(source: Path) -> None:
    if not (source / "codex-rs" / "Cargo.toml").exists() or not (source / ".git").exists():
        raise ValueError(f"not an OpenAI Codex checkout: {source}")


def apply_patch(source: Path, *, check: bool = False) -> None:
    validate_codex_source(source)
    # Legacy exports are kept for old integrations, not as the current build source.
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True
    ).strip()
    if revision != PINNED_CODEX_COMMIT:
        raise ValueError(
            "Legacy patch exports require their exact upstream base "
            f"{PINNED_CODEX_COMMIT}. Current source builds use the pinned atiti/codex "
            "commit stack through scripts/install.sh; see docs/codex-fork.md."
        )
    for codex_patch in patch_paths():
        command = ["git", "apply", "--recount"]
        if check:
            command.append("--check")
        command.append(str(codex_patch))
        subprocess.run(command, cwd=source, check=True)


def build_codex(source: Path, *, release: bool = True) -> Path:
    validate_codex_source(source)
    command = ["cargo", "build", "-p", "codex-cli", "--bin", "codex"]
    profile = "debug"
    if release:
        command.append("--release")
        profile = "release"
    subprocess.run(command, cwd=source / "codex-rs", check=True)
    binary = source / "codex-rs" / "target" / profile / "codex"
    if not binary.exists():
        raise RuntimeError(f"Codex build did not produce {binary}")
    return binary


def install_binary(binary: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        shutil.copy2(binary, temporary_path)
        temporary_path.chmod(0o755)
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return destination
