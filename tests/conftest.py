import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime_homes(monkeypatch):
    """Tests must never read or mutate the developer's live routing/session state."""
    with tempfile.TemporaryDirectory(prefix="ar-", dir="/tmp") as home:
        root = Path(home)
        monkeypatch.setenv("AGENTROUTE_HOME", str(root / "a"))
        monkeypatch.setenv("CODEX_HOME", str(root / "c"))
        yield
