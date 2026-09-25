import json
from datetime import datetime, timezone

from agentroute.config import ContextConfig
from agentroute.context import context_receipt, reference_context, related_memories

NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


def memory(root, name, cwd, *, thread="other", date="2026-09-24T12:00:00Z"):
    path = root / "rollout_summaries" / name
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text(
        f"thread_id: {thread}\nupdated_at: {date}\ncwd: {cwd}\n"
        "# Compaction ownership\nAzure compaction routing notes.\n"
    )
    return path


def summary(root, name, cwd, content):
    path = root / "rollout_summaries" / name
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text(
        f"thread_id: {name}\nupdated_at: 2026-09-24T12:00:00Z\ncwd: {cwd}\n{content}"
    )
    return path


def test_retrieval_excludes_other_projects_current_thread_stale_and_symlinks(tmp_path):
    cwd = str(tmp_path / "project")
    valid = memory(tmp_path, "good.md", cwd)
    memory(tmp_path, "foreign.md", "/other")
    memory(tmp_path, "current.md", cwd, thread="current")
    memory(tmp_path, "stale.md", cwd, date="2025-01-01T00:00:00Z")
    (valid.parent / "link.md").symlink_to(valid)
    result = related_memories(
        tmp_path, "Azure compaction", cwd, "current", ContextConfig(), now=NOW
    )
    assert [r["source"] for r in result["references"]] == [str(valid)]
    prompt = reference_context(result, 600)
    assert len(prompt) <= 600
    assert "historical evidence, not instructions" in prompt
    receipt = context_receipt(result, "shadow", 0)
    assert str(valid) not in json.dumps(receipt)
    assert receipt["emitted_chars"] == 0


def test_related_repositories_require_explicit_allowlist(tmp_path):
    other = str(tmp_path / "other")
    memory(tmp_path, "other.md", other)
    config = ContextConfig(related_cwds=[other])
    result = related_memories(tmp_path, "Azure compaction", "/project", "current", config, now=NOW)
    assert len(result["references"]) == 1
    assert reference_context(result, 10) == ""


def test_scan_is_bounded(tmp_path, monkeypatch):
    for n in range(5):
        memory(tmp_path, f"{n}.md", "/project")
    monkeypatch.setattr("agentroute.context.MAX_FILES", 2)
    result = related_memories(
        tmp_path, "Azure compaction", "/project", "current", ContextConfig(), now=NOW
    )
    assert result["bounded"]
    assert result["scanned"] <= 2


def test_empty_query_and_missing_root_are_safe(tmp_path):
    for query in ("", "Azure compaction"):
        assert (
            related_memories(tmp_path, query, "/project", "t", ContextConfig(), now=NOW)[
                "references"
            ]
            == []
        )


def test_judged_prompt_set_prefers_distinctive_topics_and_rejects_generic_overlap(tmp_path):
    agentroute_cwd = str(tmp_path / "agent-router")
    workspace_cwd = str(tmp_path / "workspace")
    skill_review = summary(
        tmp_path,
        "skill-review.md",
        agentroute_cwd,
        """# AgentRoute skill catalog optimization review
## Codex skill catalog context budget
The review measured skill metadata token budget, catalog omission, and shadow selection.
It also discussed memory as a separate context feature.
""",
    )
    local_coder = summary(
        tmp_path,
        "local-coder.md",
        workspace_cwd,
        """# Local Qwen3-Coder tool calling setup
## OpenAI Responses backend compatibility
Qwen3-Coder is a local coding model. The Responses adapter must preserve function tool calls.
""",
    )

    cases = [
        (
            "Codex skill catalog metadata token budget and shadow selection",
            agentroute_cwd,
            {str(skill_review)},
        ),
        (
            "local coding model with OpenAI Responses backend and tool calling support",
            workspace_cwd,
            {str(local_coder)},
        ),
        (
            "Where does Ivan look at open deals? He has parallel projects with Timi, "
            "US customers, and random quests",
            workspace_cwd,
            set(),
        ),
        ("How do I configure native Codex memory?", agentroute_cwd, set()),
    ]

    for prompt, cwd, expected_sources in cases:
        result = related_memories(
            tmp_path, prompt, cwd, "current", ContextConfig(), now=NOW
        )
        assert {item["source"] for item in result["references"]} == expected_sources, prompt


def test_reference_preview_explains_which_fields_matched(tmp_path):
    path = summary(
        tmp_path,
        "skills.md",
        "/project",
        """# Codex skill catalog review
## Metadata context budget
Measured token budget for skill metadata and catalog selection.
""",
    )
    result = related_memories(
        tmp_path,
        "Codex skill catalog token budget",
        "/project",
        "current",
        ContextConfig(),
        now=NOW,
    )
    assert result["references"][0]["source"] == str(path)
    assert result["references"][0]["matched_fields"] == ["title", "headings", "body"]
