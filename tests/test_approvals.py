from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentroute import approvals
from agentroute.cli import app


@pytest.fixture(autouse=True)
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path / "agentroute"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))


def test_install_is_idempotent_and_preserves_user_rules_and_extras(tmp_path):
    path = approvals.rules_path()
    path.parent.mkdir(parents=True)
    path.write_text('prefix_rule(pattern=["git", "push"], decision="allow")\n')
    tool = tmp_path / "work_packets.py"
    tool.write_text("print('ok')\n")
    _, first = approvals.install([["python3", str(tool)]])
    once = path.read_text()
    _, second = approvals.install()
    assert first == second and path.read_text() == once
    assert once.startswith('prefix_rule(pattern=["git", "push"], decision="allow")')
    assert once.count(approvals.BEGIN) == 1
    assert ["python3", str(tool)] in approvals.allowed_patterns()
    assert ["gh", "pr", "checks"] in approvals.allowed_patterns()
    assert approvals.uninstall() and approvals.BEGIN not in path.read_text()
    assert ["git", "push"] in approvals.allowed_patterns()


def test_nothing_that_mutates_or_runs_supplied_code_is_allowlisted():
    patterns = [pattern for pattern, _ in approvals.routine_patterns()]
    joined = [" ".join(pattern) for pattern in patterns]
    for forbidden in (
        "gh api", "gh pr merge", "gh pr create", "gh issue comment", "gh run rerun",
        "gh workflow run", "agentroute wake", "agentroute wait command", "agentroute wait url",
        "agentroute wait fire", "agentroute herdr team run", "agentroute herdr team dispatch",
    ):  # fmt: skip
        assert not any(forbidden.startswith(item) or item.startswith(forbidden) for item in joined)
    for interpreter in approvals.INTERPRETERS | {"rg", "sort", "sed", "awk", "xargs", "tee"}:
        assert [interpreter] not in patterns


@pytest.mark.parametrize(
    "extra",
    [["python3"], ["python3", "-c"], ["bash", "script.sh"], ["python3", "~/tool.py"], ["rm"],
     ["curl"], ["tool", "a*"], []],
)  # fmt: skip
def test_extra_rules_reject_interpreters_wildcards_and_destructive_tools(extra):
    with pytest.raises(ValueError):
        approvals.validate_extra(extra)


def test_explain_names_the_construct_that_forces_a_review():
    approvals.install()
    assert approvals.explain("gh pr view 179 --repo atiti/agent-router --json state")[
        "runs_without_review"
    ]
    assert approvals.explain("gh pr checks 179 | head -20")["runs_without_review"]
    assert approvals.explain("agentroute wait pr 'atiti/agent-router#179'")["runs_without_review"]
    for command, word in [
        ("python3 ~/workspace/tools/scripts/work_packets.py --namespace private startup", "~"),
        ("gh pr view 179 2>&1", "redirection"),
        ("gh pr view $PR", "variables"),
        ("agentroute wait pr atiti/agent-router#179", "single-quote"),
        ("GH_PAGER=cat gh pr view 179", "environment"),
        ("gh pr merge 179 --squash", "no allow rule"),
        ("gh pr view 179 | python3 -c 'print(1)'", "no allow rule"),
    ]:
        result = approvals.explain(command)
        assert not result["runs_without_review"] and word in " ".join(result["problems"]), command


def test_gh_get_only_issues_get_requests(monkeypatch):
    calls = []
    monkeypatch.setattr(
        approvals.subprocess, "run",
        lambda argv, check=False: calls.append(argv) or subprocess.CompletedProcess(argv, 0),
    )  # fmt: skip
    runner = CliRunner()
    assert (
        runner.invoke(
            app,
            ["gh-get", "repos/atiti/agent-router/actions/runs?per_page=5", "--jq", ".total_count"],
        ).exit_code
        == 0
    )
    assert calls == [
        [
            "gh",
            "api",
            "--method",
            "GET",
            "repos/atiti/agent-router/actions/runs?per_page=5",
            "--jq",
            ".total_count",
        ]
    ]
    for rejected in ("graphql", "--method=POST", "repos/a/b -X DELETE"):
        assert runner.invoke(app, ["gh-get", "--", rejected]).exit_code != 0
    assert len(calls) == 1


def test_generated_rules_are_accepted_by_codex_execpolicy(tmp_path):
    codex = shutil.which("codex-bin") or shutil.which("codex")
    if codex is None:
        pytest.skip("codex binary unavailable")
    path, _ = approvals.install()

    def decision(*command: str) -> str:
        result = subprocess.run(
            [codex, "execpolicy", "check", "--rules", str(path), *command],
            capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout).get("decision", "none")

    assert decision("gh", "pr", "checks", "179") == "allow"
    assert decision("agentroute", "wait", "timer", "20m") == "allow"
    assert decision("gh", "pr", "merge", "179") != "allow"
    assert Path(path).read_text().count("prefix_rule(") > 40
