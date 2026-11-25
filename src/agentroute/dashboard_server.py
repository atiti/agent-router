"""Loopback web server for ``agentroute dashboard``.

Live updates use server-sent events. Packet replies use the durable local wake queue.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import secrets
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import dashboard_actions
from .dashboard import DEFAULT_WINDOW, Collector

DEFAULT_PORT = 8719
PUSH_SECONDS = 2.0
ACTIONS = "/api/packet-actions"
MAX_ACTION_BODY = 24_576
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
    action_token: str | None
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
            self._page(page.read_bytes())
        elif url.path == ACTIONS:
            self._actions_get(query)
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

    def _page(self, body: bytes) -> None:
        scripts = re.findall(rb"<script>(.*?)</script>", body, re.DOTALL)
        hashes = " ".join(
            "'sha256-" + base64.b64encode(hashlib.sha256(script).digest()).decode() + "'"
            for script in scripts
        )
        script_policy = hashes or "'none'"
        self._send(HTTPStatus.OK, "text/html; charset=utf-8", body, csp=(
            "default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; "
            f"img-src 'self' data:; script-src {script_policy}; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        ))

    def _action_allowed(self) -> bool:
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        tokens = self.headers.get_all("X-AgentRoute-Token", [])
        local = ("127.0.0.1", "localhost", "[::1]")
        authorities = {f"{name}:{self.server.server_port}" for name in local}
        if self.server.server_port == 80:
            authorities.update(local)
        if (not self.action_token or not ipaddress.ip_address(self.client_address[0]).is_loopback
                or len(hosts) != 1 or hosts[0] not in authorities
                or (origins and origins != [f"http://{hosts[0]}"])
                or self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}):
            return False
        return (len(tokens) == 1 and tokens[0].isascii()
                and secrets.compare_digest(tokens[0], self.action_token))

    def _actions_get(self, query: dict) -> None:
        if not self._action_allowed():
            self._json({"error": "Packet actions are available from the local dashboard."},
                       HTTPStatus.FORBIDDEN)
            return
        try:
            fields = {key: values[0] for key, values in query.items()}
            if not fields:
                self._json({"enabled": True})
            elif set(fields) == {"id"}:
                self._json(dashboard_actions.lookup(fields["id"]))
            else:
                key = dashboard_actions.packet_key(fields)
                self._json({"receipts": dashboard_actions.history(key)})
        except dashboard_actions.ActionError as error:
            self._json({"error": str(error)}, error.status)

    def do_POST(self) -> None:  # noqa: N802
        self.close_connection = True
        if self.path != ACTIONS:
            self._json({"error": "Unknown action."}, HTTPStatus.NOT_FOUND)
            return
        if not self._action_allowed() or not self.headers.get("Origin"):
            self._json({"error": "Open the local dashboard before sending a reply."},
                       HTTPStatus.FORBIDDEN)
            return
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") or len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,5}", lengths[0])
                or not 0 < int(lengths[0]) <= MAX_ACTION_BODY
                or self.headers.get_content_type() != "application/json"):
            self._json({"error": "Send a bounded JSON reply."}, HTTPStatus.BAD_REQUEST)
            return
        try:
            self.connection.settimeout(5)
            payload = json.loads(self.rfile.read(int(lengths[0])).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("reply must be an object")
        except (ValueError, TimeoutError):
            self._json({"error": "The reply body is not valid UTF-8 JSON."}, HTTPStatus.BAD_REQUEST)
            return
        try:
            self._json(dashboard_actions.submit(payload), HTTPStatus.ACCEPTED)
        except dashboard_actions.ActionError as error:
            self._json({"error": str(error)}, error.status)
        except OSError:
            self._json({"error": "The reply could not be confirmed. Retry this submission."},
                       HTTPStatus.SERVICE_UNAVAILABLE)

    def _send(self, status: int, content_type: str, body: bytes, *, csp: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if csp:
            self.send_header("Content-Security-Policy", csp)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value: Any, status: int = HTTPStatus.OK) -> None:
        self._send(status, "application/json", json.dumps(value, default=str).encode())

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
        "action_token": secrets.token_urlsafe(32) if host in LOOPBACK_NAMES else None,
    })
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(host: str, port: int, open_browser: bool, names: list[str] | None = None) -> str:
    server = make_server(host, port, Collector(names))
    shown = "127.0.0.1" if host in {"0.0.0.0", ""} else f"[{host}]" if ":" in host else host
    url = f"http://{shown}:{server.server_address[1]}/"
    token = server.RequestHandlerClass.action_token
    operator_url = f"{url}#access={token}" if token else url
    print(f"AgentRoute dashboard: {url}  (Ctrl+C stops it)", flush=True)
    if token and not open_browser:
        print(f"Open this link to enable packet replies: {operator_url}", flush=True)
    if open_browser:
        webbrowser.open(operator_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return url
