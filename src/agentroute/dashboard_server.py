"""Loopback web server for ``agentroute dashboard``.

The page and its JSON are read-only. Live updates use server-sent events: each connection
receives only the sections of the snapshot that changed since its previous message.
"""

from __future__ import annotations

import hashlib
import json
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .dashboard import DEFAULT_WINDOW, Collector

DEFAULT_PORT = 8719
PUSH_SECONDS = 2.0
LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}
SECTIONS = ("summary", "focus", "agents", "attention", "waits", "packets", "usage", "sources")


def changed_sections(snapshot: dict[str, Any], sent: dict[str, str]) -> dict[str, Any]:
    """Sections whose content differs from what this connection last received."""
    changed = {}
    for name in SECTIONS:
        encoded = json.dumps(snapshot[name], sort_keys=True, default=str)
        digest = hashlib.sha1(encoded.encode(), usedforsecurity=False).hexdigest()
        if sent.get(name) != digest:
            sent[name] = digest
            changed[name] = snapshot[name]
    return changed


class Handler(BaseHTTPRequestHandler):
    collector: Collector
    allowed_hosts: set[str]
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        # A page on another origin must not reach this server through a DNS name it controls.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in self.allowed_hosts:
            self._send(HTTPStatus.FORBIDDEN, "text/plain", b"forbidden host")
            return
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        try:
            window = int(query.get("window", [DEFAULT_WINDOW])[0])
        except ValueError:
            window = DEFAULT_WINDOW
        if url.path == "/":
            page = resources.files("agentroute").joinpath("dashboard_assets/index.html")
            self._send(HTTPStatus.OK, "text/html; charset=utf-8", page.read_bytes())
        elif url.path == "/api/snapshot":
            self._json(self.collector.snapshot(window))
        elif url.path.startswith("/api/agent/"):
            agent = self.collector.agent(url.path.removeprefix("/api/agent/"), window)
            if agent is None:
                self._send(HTTPStatus.NOT_FOUND, "text/plain", b"unknown agent")
            else:
                self._json(agent)
        elif url.path == "/api/events":
            self._events(window)
        else:
            self._send(HTTPStatus.NOT_FOUND, "text/plain", b"not found")

    def _send(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value: Any) -> None:
        self._send(HTTPStatus.OK, "application/json", json.dumps(value, default=str).encode())

    def _events(self, window: int) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        sent: dict[str, str] = {}
        try:
            while True:
                snapshot = self.collector.snapshot(window)
                message = {"generated_at": snapshot["generated_at"],
                           "window": snapshot["window"], **changed_sections(snapshot, sent)}
                self.wfile.write(b"data: " + json.dumps(message, default=str).encode() + b"\n\n")
                self.wfile.flush()
                time.sleep(PUSH_SECONDS)
        except (BrokenPipeError, ConnectionError):
            return


def make_server(host: str, port: int, collector: Collector | None = None) -> ThreadingHTTPServer:
    handler = type("DashboardHandler", (Handler,), {
        "collector": collector or Collector(),
        "allowed_hosts": LOOPBACK_NAMES | {host},
    })
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(host: str, port: int, open_browser: bool, names: list[str] | None = None) -> str:
    server = make_server(host, port, Collector(names))
    shown = "127.0.0.1" if host in {"0.0.0.0", ""} else f"[{host}]" if ":" in host else host
    url = f"http://{shown}:{server.server_address[1]}/"
    print(f"AgentRoute dashboard: {url}  (Ctrl+C stops it)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return url
