"""Record what an installer put on disk so `agentroute doctor` can detect later drift.

The manifest lives at ``$AGENTROUTE_HOME/install-manifest.json``. It contains only
versions, the release tag or source commit, file digests, and the install time; it never
contains paths, credentials, or account data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

MANIFEST_NAME = "install-manifest.json"
MANIFEST_SCHEMA = 1
# Installed runtime files, relative to AGENTROUTE_HOME. Missing optional files are omitted.
RUNTIME_FILES = ("bin/codex-bin", "bin/codex-code-mode-host", "bin/codex")


def manifest_path(home: Path) -> Path:
    return home / MANIFEST_NAME


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_manifest(
    home: Path, source_kind: str, source_ref: str, dirty: bool = False
) -> dict[str, Any]:
    try:
        package_version = version("agentroute")
    except PackageNotFoundError:
        package_version = "unknown"
    build_path = home / "build-id"
    build_id = build_path.read_text(encoding="utf-8").strip() if build_path.exists() else None
    binaries = {
        relative: sha256_file(home / relative)
        for relative in RUNTIME_FILES
        if (home / relative).is_file()
    }
    return {
        "schema": MANIFEST_SCHEMA,
        "agentroute_version": package_version,
        "build_id": build_id,
        "source": {"kind": source_kind, "ref": source_ref, "dirty": bool(dirty)},
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "binaries": binaries,
    }


def write_manifest(home: Path, manifest: dict[str, Any]) -> Path:
    path = manifest_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=".install-manifest.", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
        staged = Path(handle.name)
    os.chmod(staged, 0o644)
    staged.replace(path)
    return path


def read_manifest(home: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(manifest_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--source-kind", choices=("release", "source"), required=True)
    parser.add_argument("--source-ref", default="unknown")
    parser.add_argument("--dirty", action="store_true")
    args = parser.parse_args(argv)
    write_manifest(
        args.home, build_manifest(args.home, args.source_kind, args.source_ref, args.dirty)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
