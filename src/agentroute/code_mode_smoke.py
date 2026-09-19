"""Exercise the Code Mode host far enough to initialize and run V8."""

from __future__ import annotations

import argparse
import json
import os
import select
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO

MARKER = "AGENTROUTE_CODE_MODE_OK"
MAX_FRAME_BYTES = 64 * 1024 * 1024


class SmokeError(RuntimeError):
    """Raised when the Code Mode host does not complete a real JS execution."""


def _frame(message: dict[str, object]) -> bytes:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    return struct.pack("<I", len(payload)) + payload


def _read_exact(stream: BinaryIO, length: int, deadline: float) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SmokeError("timed out waiting for Code Mode host response")
        ready, _, _ = select.select([stream], [], [], remaining)
        if not ready:
            raise SmokeError("timed out waiting for Code Mode host response")
        chunk = os.read(stream.fileno(), length - len(chunks))
        if not chunk:
            raise SmokeError("Code Mode host closed stdout before completing the smoke test")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_message(stream: BinaryIO, deadline: float) -> dict[str, object]:
    length = struct.unpack("<I", _read_exact(stream, 4, deadline))[0]
    if length > MAX_FRAME_BYTES:
        raise SmokeError(f"Code Mode host returned an oversized frame ({length} bytes)")
    try:
        message = json.loads(_read_exact(stream, length, deadline))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeError(f"Code Mode host returned invalid JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise SmokeError("Code Mode host returned a non-object message")
    return message


def _write_message(stream: BinaryIO, message: dict[str, object]) -> None:
    stream.write(_frame(message))
    stream.flush()


def smoke_code_mode_host(binary: Path, *, timeout: float = 15.0) -> None:
    """Run a framed handshake, open a session, and execute one JavaScript cell."""
    process = subprocess.Popen(
        [str(binary)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    try:
        _write_message(
            process.stdin,
            {
                "type": "connection/hello",
                "supportedVersions": [1],
                "requiredCapabilities": [],
                "optionalCapabilities": [],
            },
        )
        hello = _read_message(process.stdout, deadline)
        if hello.get("type") != "connection/ready":
            raise SmokeError(f"Code Mode handshake failed: {hello!r}")

        _write_message(
            process.stdin,
            {
                "type": "operation/request",
                "id": 1,
                "request": {"method": "session/open", "sessionId": "agentroute-smoke"},
            },
        )
        opened = _read_message(process.stdout, deadline)
        if opened.get("type") != "operation/response" or opened.get("id") != 1:
            raise SmokeError(f"Code Mode session did not open: {opened!r}")
        if opened.get("result", {}).get("status") != "ok":
            raise SmokeError(f"Code Mode session open failed: {opened!r}")

        _write_message(
            process.stdin,
            {
                "type": "operation/request",
                "id": 2,
                "request": {
                    "method": "session/execute",
                    "sessionId": "agentroute-smoke",
                    "request": {
                        "tool_call_id": "agentroute-smoke",
                        "enabled_tools": [],
                        "source": f'text("{MARKER}");',
                        "yield_time_ms": 1000,
                        "max_output_tokens": 64,
                    },
                },
            },
        )
        started = _read_message(process.stdout, deadline)
        if started.get("type") != "operation/response" or started.get("id") != 2:
            raise SmokeError(f"Code Mode JavaScript execution did not start: {started!r}")
        if started.get("result", {}).get("status") != "ok":
            raise SmokeError(f"Code Mode JavaScript start failed: {started!r}")

        response = _read_message(process.stdout, deadline)
        if response.get("type") != "execute/initialResponse" or response.get("id") != 2:
            raise SmokeError(f"Code Mode JavaScript result was not returned: {response!r}")
        if response.get("result", {}).get("status") != "ok":
            raise SmokeError(f"Code Mode JavaScript execution failed: {response!r}")
        if MARKER not in json.dumps(response, separators=(",", ":")):
            raise SmokeError(f"Code Mode JavaScript output omitted the marker: {response!r}")
    except (BrokenPipeError, OSError) as exc:
        raise SmokeError(f"Code Mode host IPC failed: {exc}") from exc
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process.returncode not in (0, -15) and sys.exc_info()[0] is None:
            stderr = (
                process.stderr.read().decode("utf-8", errors="replace")
                if process.stderr
                else ""
            )
            raise SmokeError(
                f"Code Mode host exited with status {process.returncode}: {stderr.strip()}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)
    try:
        smoke_code_mode_host(args.binary, timeout=args.timeout)
    except SmokeError as exc:
        print(f"Code Mode smoke test failed: {exc}", file=sys.stderr)
        return 1
    print(f"Code Mode JavaScript smoke test passed: {args.binary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
