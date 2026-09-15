from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

PINNED_CODEX_COMMIT = "b0af519c39766c173191fc39b341808619b51c74"


def patch_path() -> Path:
    return Path(__file__).with_name("patches") / "codex-user-prompt-model-override.patch"


def validate_codex_source(source: Path) -> None:
    if not (source / "codex-rs" / "Cargo.toml").exists() or not (source / ".git").exists():
        raise ValueError(f"not an OpenAI Codex checkout: {source}")


def apply_patch(source: Path, *, check: bool = False) -> None:
    validate_codex_source(source)
    command = ["git", "apply"]
    if check:
        command.append("--check")
    command.append(str(patch_path()))
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
    shutil.copy2(binary, destination)
    destination.chmod(0o755)
    return destination
