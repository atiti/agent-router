import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_voice_push_to_talk_and_receipt_ui():
    subprocess.run(
        ["node", "tests/voice_ui.cjs", "src/agentroute/dashboard_assets/voice.html"],
        check=True, timeout=5,
    )
