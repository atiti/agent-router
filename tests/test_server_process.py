import json
import subprocess
import sys

import pytest

from agentroute.server_process import MIN_OPEN_FILES, prepare_open_file_limit

resource = pytest.importorskip("resource")


@pytest.mark.parametrize(
    ("soft", "hard", "expected"),
    [
        (256, resource.RLIM_INFINITY, (MIN_OPEN_FILES, resource.RLIM_INFINITY)),
        (256, 1024, (1024, 1024)),
        (256, 256, None),
        (MIN_OPEN_FILES, resource.RLIM_INFINITY, None),
        (resource.RLIM_INFINITY, resource.RLIM_INFINITY, None),
    ],
)
def test_only_raise_soft_limit_within_existing_hard_limit(monkeypatch, soft, hard, expected):
    calls = []
    monkeypatch.setattr(resource, "getrlimit", lambda limit: (soft, hard))
    monkeypatch.setattr(resource, "setrlimit", lambda limit, values: calls.append(values))
    prepare_open_file_limit()
    assert calls == ([] if expected is None else [expected])


def test_limit_failure_is_reported_without_preventing_start(monkeypatch, capsys):
    monkeypatch.setattr(resource, "getrlimit", lambda limit: (256, resource.RLIM_INFINITY))

    def denied(limit, values):
        raise OSError("denied")

    monkeypatch.setattr(resource, "setrlimit", denied)
    prepare_open_file_limit()
    assert "could not raise shared server open-file limit: denied" in capsys.readouterr().err


def test_bootstrap_raises_only_child_limit():
    original = resource.getrlimit(resource.RLIMIT_NOFILE)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentroute.server_process",
            sys.executable,
            "-c",
            "import json, resource; print(json.dumps(resource.getrlimit(resource.RLIMIT_NOFILE)))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    soft, hard = json.loads(result.stdout)
    assert hard == original[1]
    assert soft == resource.RLIM_INFINITY or soft >= (
        MIN_OPEN_FILES if hard == resource.RLIM_INFINITY else min(MIN_OPEN_FILES, hard)
    )
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == original
