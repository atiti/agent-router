import json
from unittest.mock import patch

import pytest

from agentroute.memory_profile import parse_processes, watch
from agentroute.playwright_memory import install_guard


def test_process_parser_preserves_executable_spaces_without_arguments():
    assert parse_processes("123 1 100 /Applications/Codex App/codex-bin\n") == [
        {
            "pid": 123,
            "ppid": 1,
            "rss_bytes": 102400,
            "executable": "/Applications/Codex App/codex-bin",
        }
    ]
    assert parse_processes("PID PPID RSS COMM\n") == []


def test_watch_is_bounded_and_does_not_store_other_application_inventory(tmp_path):
    sample = {
        "timestamp": 1,
        "platform": "linux",
        "processes": [
            {"pid": 123, "ppid": 1, "rss_bytes": 10, "executable": "codex-bin"},
            {"pid": 456, "ppid": 1, "rss_bytes": 10, "executable": "unrelated-app"},
        ],
    }
    with (
        patch("agentroute.memory_profile.snapshot", return_value=sample),
        patch("agentroute.memory_profile.time.monotonic", side_effect=[0, 2]),
    ):
        result = watch(duration=1, interval=1, threshold_bytes=100, output=tmp_path)
    assert result["samples"] == 1
    saved = json.loads((tmp_path / "samples.jsonl").read_text())
    assert [p["pid"] for p in saved["processes"]] == [123]
    assert (tmp_path / "samples.jsonl").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("duration,interval", [(0, 1), (86401, 1), (1, 0)])
def test_watch_rejects_unbounded_or_busy_polling(duration, interval, tmp_path):
    with pytest.raises(ValueError):
        watch(duration=duration, interval=interval, threshold_bytes=100, output=tmp_path)


def test_playwright_installer_preserves_original_and_is_repeatable(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTROUTE_HOME", str(tmp_path / "home"))
    original = tmp_path / "original cli"
    original.write_text("#!/bin/sh\nexit 0\n")
    original.chmod(0o755)
    receipt = install_guard(original)
    assert receipt["original"] == str(original)
    assert "NODE_OPTIONS" in (tmp_path / "home/bin/playwright-cli").read_text()
    assert original.read_text() == "#!/bin/sh\nexit 0\n"
    assert install_guard(original) == receipt
