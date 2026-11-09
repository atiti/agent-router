import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime_homes(monkeypatch):
    """Tests must never read or mutate the developer's live routing/session state."""
    monkeypatch.delenv("AGENTROUTE_CONFIG", raising=False)
    monkeypatch.delenv("AGENTROUTE_DATA_DIR", raising=False)
    monkeypatch.delenv("AGENTROUTE_RUNTIME_BUILD_ID", raising=False)
    monkeypatch.delenv("AGENTROUTE_CODEX_SOURCE", raising=False)
    # Launcher tests must opt into mocked pane control, even when pytest runs in Herdr.
    for name in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH",
                 "AGENTROUTE_HERDR_TEAM", "AGENTROUTE_HERDR_MEMBER"):
        monkeypatch.delenv(name, raising=False)
    with tempfile.TemporaryDirectory(prefix="ar-", dir="/tmp") as home:
        root = Path(home)
        monkeypatch.setenv("AGENTROUTE_HOME", str(root / "a"))
        monkeypatch.setenv("CODEX_HOME", str(root / "c"))
        yield
