import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_preload_caps_real_collector_patterns_without_mutating_package(tmp_path):
    bundle = tmp_path / "playwright-core/lib/coreBundle.js"
    bundle.parent.mkdir(parents=True)
    source = """let Tab; const EventEmitter = require('node:events').EventEmitter;
Tab = class _Tab extends EventEmitter {
  constructor() { super(); this._requests = []; this._recentEventEntries = []; }
  _handleRequest(request2) { this._requests.push(request2); }
  _addLogEntry(entry) { this._recentEventEntries.push(entry); }
  async captureSnapshot() {}
}; module.exports = Tab;"""
    bundle.write_text(source)
    guard = Path(__file__).parents[1] / "src/agentroute/playwright_memory_guard.cjs"
    program = """require(process.argv[1]); const Tab=require(process.argv[2]); const t=new Tab();
for(let i=0;i<10000;i++){t._handleRequest(i); t._addLogEntry(i);}
process.stdout.write(JSON.stringify([t._requests.length,t._recentEventEntries.length,
t._requests.at(-1),t._recentEventEntries.at(-1)]));"""
    result = subprocess.run(
        ["node", "-e", program, str(guard), str(bundle)], capture_output=True, text=True, check=True
    )
    requests, events, last_request, last_event = json.loads(result.stdout)
    assert requests <= 1024
    assert events <= 2048
    assert (last_request, last_event) == (9999, 9999)
    assert bundle.read_text() == source
