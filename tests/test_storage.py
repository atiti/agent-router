import os
import time

import pytest

from agentroute.storage import prune_desktop_backups, prune_diagnostics, prune_group


def test_prune_oldest_first_and_preserve_recent_writer(tmp_path):
    now = time.time()
    paths = []
    for name, age in (("old", 7200), ("new", 4000), ("active", 1)):
        path = tmp_path / name
        path.write_bytes(b"abc")
        os.utime(path, (now - age, now - age))
        paths.append(path)
    assert prune_group(paths, 6, 7, now=now) == {"removed_bytes": 3, "remaining_bytes": 6}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["active", "new"]


def test_prune_never_follows_root_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "profiling"
    root.symlink_to(outside)
    with pytest.raises(ValueError):
        prune_diagnostics(root)


def test_reconcile_quota_after_pruning(tmp_path):
    root = tmp_path / "profiling"
    bundle = root / "traces" / "trace-old"
    bundle.mkdir(parents=True)
    payload = bundle / "trace.jsonl"
    payload.write_bytes(b"abc")
    old = time.time() - 8 * 86400
    os.utime(payload, (old, old))
    assert prune_diagnostics(root)["traces"] == {"removed_bytes": 3, "remaining_bytes": 0}
    assert int.from_bytes((root / "traces" / ".budget").read_bytes(), "little") == 0


def test_desktop_retains_two_latest_and_ignores_other_backups(tmp_path):
    root = tmp_path / "backups" / "desktop"
    root.mkdir(parents=True)
    for index in range(4):
        app = root / f"ChatGPT-Routed-{index}.app"
        app.mkdir()
        (app / "binary").write_bytes(b"abc")
        os.utime(app, (index, index))
    (root / "unrelated.app").mkdir()
    assert prune_desktop_backups(tmp_path) == 6
    assert sorted(p.name for p in root.iterdir()) == [
        "ChatGPT-Routed-2.app",
        "ChatGPT-Routed-3.app",
        "unrelated.app",
    ]
