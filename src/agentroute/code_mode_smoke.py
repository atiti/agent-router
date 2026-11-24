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
import socket
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
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                # Preserve the original failure if the host closed its IPC pipe first.
                pass
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


def smoke_grpc_host(binary: Path, *, timeout: float = 15.0) -> None:
    """Open an upstream gRPC session and verify a JavaScript result."""
    import grpc
    from agentroute.code_mode_proto import v1_pb2 as proto

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process = subprocess.Popen(
        [str(binary), "--listen", f"grpc://127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        grpc.channel_ready_future(channel).result(timeout=timeout)
        prefix = "/codex.code_mode.v1.CodeModeHost/"
        open_session = channel.unary_stream(
            prefix + "OpenSession", request_serializer=proto.OpenSessionRequest.SerializeToString,
            response_deserializer=proto.SessionEvent.FromString,
        )
        execute = channel.unary_stream(
            prefix + "Execute", request_serializer=proto.ExecuteRequest.SerializeToString,
            response_deserializer=proto.ExecuteEvent.FromString,
        )
        lease = open_session(proto.OpenSessionRequest(), timeout=timeout)
        opened = next(lease)
        if not opened.HasField("opened") or not opened.opened.session_id:
            raise SmokeError("Code Mode session did not open")
        request = proto.ExecuteRequest(
            session_id=opened.opened.session_id, execution_id="smoke",
            tool_call_id="smoke", source=f'text("{MARKER}");',
            yield_time_ms=1000, max_output_tokens=64,
        )
        for event in execute(request, timeout=timeout):
            if event.HasField("outcome"):
                outcome = event.outcome
                if (outcome.HasField("completed") and not outcome.completed.HasField("error_text")
                        and any(item.HasField("text") and MARKER in item.text.text
                                for item in outcome.content_items)):
                    return
                raise SmokeError("Code Mode JavaScript execution failed or returned no marker")
        raise SmokeError("Code Mode host returned no JavaScript result")
    except (grpc.RpcError, grpc.FutureTimeoutError, StopIteration, OSError) as exc:
        raise SmokeError(f"Code Mode gRPC smoke failed: {exc}") from exc
    finally:
        channel.close()
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)
    try:
        smoke_grpc_host(args.binary, timeout=args.timeout)
    except SmokeError as exc:
        print(f"Code Mode smoke test failed: {exc}", file=sys.stderr)
        return 1
    print(f"Code Mode JavaScript smoke test passed: {args.binary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
