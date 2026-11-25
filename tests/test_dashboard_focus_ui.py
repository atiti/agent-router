import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_live_focus_review_and_reply_routing():
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["node", "tests/dashboard_focus.cjs", "src/agentroute/dashboard_assets/index.html"],
        cwd=root,
        check=True,
    )
