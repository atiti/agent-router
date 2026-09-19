from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path

import pytest

from agentroute.code_mode_smoke import SmokeError, smoke_code_mode_host


def _decode_frame(data: bytes, offset: int) -> tuple[dict[str, object], int]:
    length = struct.unpack("<I", data[offset : offset + 4])[0]
    start = offset + 4
    end = start + length
    return json.loads(data[start:end]), end


def _encode_frame(message: dict[str, object]) -> bytes:
    payload = json.dumps(message).encode()
    return struct.pack("<I", len(payload)) + payload


def test_smoke_executes_real_javascript_cell(tmp_path: Path):
    host = tmp_path / "host.py"
    host.write_text(
        """#!/usr/bin/env python3
import json, struct, sys
def read():
    size = struct.unpack('<I', sys.stdin.buffer.read(4))[0]
    return json.loads(sys.stdin.buffer.read(size))
def write(value):
    payload = json.dumps(value).encode()
    sys.stdout.buffer.write(struct.pack('<I', len(payload)) + payload)
    sys.stdout.buffer.flush()
hello = read()
assert hello['type'] == 'connection/hello'
write({'type':'connection/ready','selectedVersion':1,'capabilities':[]})
opened = read()
write({'type':'operation/response','id':opened['id'],'result':{'status':'ok','value':{'type':'session/ready','sessionId':'agentroute-smoke'}}})
execute = read()
assert execute['request']['method'] == 'session/execute'
assert 'AGENTROUTE_CODE_MODE_OK' in execute['request']['request']['source']
write({'type':'operation/response','id':execute['id'],'result':{'status':'ok','value':{'type':'execution/started','cellId':'1'}}})
write({'type':'execute/initialResponse','id':execute['id'],'result':{'status':'ok','value':{'Terminated':{'cell_id':'1','content_items':[{'type':'input_text','text':'AGENTROUTE_CODE_MODE_OK'}],'code_mode_host_duration_ns':1}}}})
""",
        encoding="utf-8",
    )
    host.chmod(0o755)

    smoke_code_mode_host(host, timeout=2)


def test_smoke_rejects_host_that_dies_before_javascript_result(monkeypatch):
    stdout = b"".join(
        [
            _encode_frame(
                {"type": "connection/ready", "selectedVersion": 1, "capabilities": []}
            ),
            _encode_frame(
                {
                    "type": "operation/response",
                    "id": 1,
                    "result": {
                        "status": "ok",
                        "value": {"type": "session/ready", "sessionId": "agentroute-smoke"},
                    },
                }
            ),
            _encode_frame(
                {
                    "type": "operation/response",
                    "id": 2,
                    "result": {
                        "status": "ok",
                        "value": {"type": "execution/started", "cellId": "1"},
                    },
                }
            ),
        ]
    )
    host = subprocess.Popen(
        ["python3", "-c", f"import sys; sys.stdout.buffer.write({stdout!r})"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: host)

    with pytest.raises(SmokeError, match="closed stdout"):
        smoke_code_mode_host(Path("unused"), timeout=2)
