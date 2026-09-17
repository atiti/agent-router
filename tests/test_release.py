from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from agentroute import release


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "agentroute-darwin-arm64.tar.gz"),
        ("Linux", "x86_64", "agentroute-linux-x64.tar.gz"),
    ],
)
def test_release_asset_name(system, machine, expected, monkeypatch):
    monkeypatch.setattr(release.platform, "system", lambda: system)
    monkeypatch.setattr(release.platform, "machine", lambda: machine)
    assert release.release_asset_name() == expected


def test_release_archive_rejects_parent_traversal(tmp_path: Path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        item = tarfile.TarInfo("../escape")
        item.size = 1
        bundle.addfile(item, io.BytesIO(b"x"))
    with pytest.raises(RuntimeError, match="unsafe path"):
        release._safe_extract(archive, tmp_path / "payload")
