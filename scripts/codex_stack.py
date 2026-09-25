#!/usr/bin/env python3
"""Prepare an isolated stable-release rebase; never update a published branch or install."""

import argparse
import json
import re
import subprocess
from pathlib import Path


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def prepare(repo, base, tip, onto, branch, worktree):
    """Resolve all inputs before creating a worktree. Conflicts stay there for review."""
    if worktree.exists():
        raise ValueError("candidate worktree must not already exist")
    resolved = [
        git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}") for ref in (base, tip, onto)
    ]
    old_base, old_tip, new_base = resolved
    subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", old_base, old_tip], check=True
    )
    git(repo, "check-ref-format", "--branch", branch)
    if not branch.startswith("agentroute-port-"):
        raise ValueError("candidate branch must start with agentroute-port-")
    if git(repo, "branch", "--list", branch):
        raise ValueError("candidate branch already exists")
    git(repo, "worktree", "add", "-b", branch, str(worktree), old_tip)
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            "rerere.enabled=true",
            "rebase",
            "--onto",
            new_base,
            old_base,
        ]
    )
    return {
        "status": "needs_conflict_review" if completed.returncode else "needs_validation",
        "base": old_base,
        "original_tip": old_tip,
        "candidate_base": new_base,
        "branch": branch,
        "worktree": str(worktree),
        "review_command": f"git range-diff {old_base}..{old_tip} {new_base}..{branch}",
        "published": False,
        "installed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument(
        "--base", required=True, help="Exact OLD upstream base, not merge-base(main)"
    )
    parser.add_argument("--tip", required=True, help="Reviewed downstream tip")
    parser.add_argument("--onto", required=True, help="Locally fetched upstream release tag or SHA")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--worktree", required=True, type=Path)
    args = parser.parse_args()
    # Reject option-like refs before handing values to Git revision parsing.
    for ref in (args.base, args.tip, args.onto):
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", ref) or ref.startswith("-"):
            parser.error("expected a plain ref or commit ID")
    try:
        result = prepare(
            args.repo.resolve(),
            args.base,
            args.tip,
            args.onto,
            args.branch,
            args.worktree.resolve(),
        )
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Rebase preparation failed: {error}\n")
    print(json.dumps(result, indent=2))
    return 1 if result["status"] == "needs_conflict_review" else 0


if __name__ == "__main__":
    raise SystemExit(main())
