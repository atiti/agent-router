from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

DEFAULT_REPOSITORY = "atiti/agent-router"


def release_asset_name() -> str:
    system = {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    machine = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "x64",
        "amd64": "x64",
    }.get(platform.machine().lower())
    if system is None or machine is None:
        raise RuntimeError(
            f"no AgentRoute release for {platform.system()} {platform.machine()}"
        )
    return f"agentroute-{system}-{machine}.tar.gz"


def _safe_extract(archive: Path, destination: Path) -> None:
    destination_resolved = destination.resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            if member.issym() or member.islnk():
                raise RuntimeError(f"release archive contains a link: {member.name}")
            if not member.isfile() and not member.isdir():
                raise RuntimeError(f"release archive contains a special file: {member.name}")
            target = (destination / member.name).resolve()
            if destination_resolved != target and destination_resolved not in target.parents:
                raise RuntimeError(f"release archive contains an unsafe path: {member.name}")
        bundle.extractall(destination)


def install_latest_release(repository: str | None = None) -> str:
    """Download, verify, and install the latest binary release for this platform."""
    repository = repository or os.environ.get("AGENTROUTE_REPOSITORY", DEFAULT_REPOSITORY)
    root = os.environ.get(
        "AGENTROUTE_DOWNLOAD_ROOT",
        f"https://github.com/{repository}/releases/latest/download",
    )
    asset = release_asset_name()
    with tempfile.TemporaryDirectory(prefix="agentroute-update-") as temporary:
        download = Path(temporary)
        archive = download / asset
        checksum = download / f"{asset}.sha256"
        urllib.request.urlretrieve(f"{root}/{asset}", archive)
        urllib.request.urlretrieve(f"{root}/{asset}.sha256", checksum)
        checksum_fields = checksum.read_text(encoding="utf-8").split()
        if not checksum_fields:
            raise RuntimeError("release checksum file is empty")
        expected = checksum_fields[0].lower()
        digest = hashlib.sha256()
        with archive.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        actual = digest.hexdigest()
        if expected != actual:
            raise RuntimeError(f"release checksum mismatch: expected {expected}, got {actual}")
        payload = download / "payload"
        payload.mkdir()
        _safe_extract(archive, payload)
        installer = payload / "install.sh"
        if not installer.is_file():
            raise RuntimeError("verified release has no installer")
        installer.chmod(0o755)
        subprocess.run([str(installer)], check=True)
        version = payload / "VERSION"
        return version.read_text(encoding="utf-8").strip() if version.exists() else "unknown"
