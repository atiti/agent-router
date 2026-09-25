import importlib.util
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "codex_stack", Path(__file__).parents[1] / "scripts/codex_stack.py"
)
stack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stack)


def run(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run(repo, "init", "-b", "main")
    run(repo, "config", "user.name", "Test")
    run(repo, "config", "user.email", "test@example.invalid")
    (repo / "base").write_text("base\n")
    run(repo, "add", ".")
    run(repo, "commit", "-m", "base")
    base = run(repo, "rev-parse", "HEAD")
    run(repo, "switch", "-c", "downstream")
    (repo / "feature").write_text("feature\n")
    run(repo, "add", ".")
    run(repo, "commit", "-m", "feature")
    tip = run(repo, "rev-parse", "HEAD")
    run(repo, "switch", "main")
    (repo / "upstream").write_text("upstream\n")
    run(repo, "add", ".")
    run(repo, "commit", "-m", "upstream")
    return repo, base, tip, run(repo, "rev-parse", "HEAD")


def test_rebase_keeps_original_branches_and_dirty_worktree(tmp_path):
    repo, base, tip, upstream = fixture_repo(tmp_path)
    (repo / "base").write_text("user uncommitted work\n")
    target = tmp_path / "candidate"
    result = stack.prepare(repo, base, tip, upstream, "agentroute-port-test", target)
    assert result["status"] == "needs_validation"
    assert run(repo, "rev-parse", "downstream") == tip
    assert run(repo, "rev-parse", "main") == upstream
    assert (repo / "base").read_text() == "user uncommitted work\n"
    assert (target / "feature").read_text() == "feature\n"
    assert (target / "upstream").read_text() == "upstream\n"
    assert not result["published"] and not result["installed"]


def test_existing_destination_is_never_overwritten(tmp_path):
    repo, base, tip, upstream = fixture_repo(tmp_path)
    with pytest.raises(ValueError, match="already exist"):
        stack.prepare(repo, base, tip, upstream, "agentroute-port-test", repo)


def test_main_cannot_be_candidate(tmp_path):
    repo, base, tip, upstream = fixture_repo(tmp_path)
    with pytest.raises(ValueError, match="must start"):
        stack.prepare(repo, base, tip, upstream, "main", tmp_path / "candidate")
